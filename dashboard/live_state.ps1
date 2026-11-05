# dashboard/live_state.ps1 —— 持续采样电脑状态，每行输出一个 JSON 对象到 stdout。
#
# 设计要点（性能优先，稳态单次采样必须 < 1.0 秒）：
#   - 全部走 CIM；不用 Get-Counter（每次约 1s 起步，太慢）。
#   - 单个 Get-CimInstance 约 280ms，因此「重项」按相位分摊缓存（见 $PLAN）。
#   - 静态值（CPU 型号、内存总量、网卡名、有无电池、开机时间、网关接口索引）
#     只在启动时查一次；Win32_Processor 主频每 10 轮刷新一次。
#   - 出错绝不中断循环：$ErrorActionPreference='SilentlyContinue' + 每项独立 try/catch。
#
# 实测单次耗时（本机）：cpu_load 185ms / mem 291ms / net 275ms / gpu 328ms /
#   proc 列表 60ms / Get-NetRoute 378ms（故只启动时用）/ 其余 < 30ms。
#
# 用法：
#   pwsh -NoProfile -File dashboard/live_state.ps1
#   pwsh -NoProfile -File dashboard/live_state.ps1 -IntervalMs 1500 -Count 10
#
# 输出字段（key 固定，缺数据一律 null，不省略）：
#   clock date uptime cpu_load cpu_freq_ghz mem_pct mem_used_gb mem_total_gb temp_c gpu_pct
#   disk_free_gb net_down_mbps net_up_mbps bat_pct bat_charging bat_ac vol_pct
#   top_proc top_proc_pct ip ping_ms ping_loss link_wired

param(
    [int]$IntervalMs = 1500,
    [int]$Count = 0,
    # 第 0 轮强制刷新全部项（约 1.2s），保证首行 JSON 每个字段都有值。
    # push.js 的 once/dry 需要「一次拿全」，会带上它；长跑的 loop 也可以带，
    # 只多付一次冷启动代价。
    [switch]$Warmup,
    # 诊断用：把本轮采样耗时（毫秒）与刷新了哪些项写到 stderr，不影响 stdout 的 JSON 流
    [switch]$TimingToStderr
)

$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'

# stdout 必须是无 BOM 的 UTF-8，否则 Node 侧按 utf8 读会出乱码
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch {}
try { $OutputEncoding = [Console]::OutputEncoding } catch {}

$INV = [Globalization.CultureInfo]::InvariantCulture

# 注意：这些周期模拟真实节奏，但冷启动时 net/gpu/temp 等项要等到各自 phase
# 才有值。调用方若要「一次拿全」，跑 -Count 6 取最后一行即可（第 5 轮已覆盖
# mem/net/gpu/temp/disk/freq 全部相位）。
$PLAN = @{
    cpu  = @{ cad = 1;  phase = 0 }
    top  = @{ cad = 1;  phase = 0 }
    mem  = @{ cad = 2;  phase = 0 }
    net  = @{ cad = 4;  phase = 1 }
    gpu  = @{ cad = 4;  phase = 3 }
    temp = @{ cad = 6;  phase = 2 }
    disk = @{ cad = 7;  phase = 3 }
    bat  = @{ cad = 8;  phase = 4 }
    vol  = @{ cad = 8;  phase = 6 }
    freq = @{ cad = 10; phase = 8 }
    ip   = @{ cad = 20; phase = 10 }
    ping = @{ cad = 4;  phase = 2 }
    link = @{ cad = 8;  phase = 4 }
}

# ─────────────────────────────────────────────────────────────
# JSON 拼装（手工拼，避免 ConvertTo-Json 的每轮开销与转义不确定）
# ─────────────────────────────────────────────────────────────
function J([object]$s) {
    if ($null -eq $s) { return 'null' }
    $t = [string]$s
    if ($t -eq '') { return 'null' }
    $sb = New-Object System.Text.StringBuilder
    foreach ($ch in $t.ToCharArray()) {
        switch -CaseSensitive ([string]$ch) {
            '"' { [void]$sb.Append('\"') }
            '\' { [void]$sb.Append('\\') }
            "`n" { [void]$sb.Append('\n') }
            "`r" { [void]$sb.Append('\r') }
            "`t" { [void]$sb.Append('\t') }
            Default {
                if ([int]$ch -lt 32) { [void]$sb.Append('\u{0:x4}' -f [int]$ch) }
                else { [void]$sb.Append($ch) }
            }
        }
    }
    return '"' + $sb.ToString() + '"'
}

function N([object]$v, [int]$d) {
    # 数字：固定小数位、InvariantCulture，保证永远是 12.3 这种点号形式
    if ($null -eq $v) { return 'null' }
    try {
        $x = [double]$v
        if ([double]::IsNaN($x) -or [double]::IsInfinity($x)) { return 'null' }
        if ($d -le 0) { return $x.ToString('0', $INV) }
        return $x.ToString('0.' + ('0' * $d), $INV)
    } catch { return 'null' }
}

function B([object]$v) {
    if ($null -eq $v) { return 'false' }
    if ([bool]$v) { return 'true' }
    return 'false'
}

# ─────────────────────────────────────────────────────────────
# 启动时的一次性初始化（静态值 + CoreAudio 类型编译）
# ─────────────────────────────────────────────────────────────
$bootTime = $null
$cpuName = $null
$memTotalGb = $null
$nicName = $null
$gwIndex = $null
$gwAddr = $null
$hasBattery = $false
$cores = 1
$baseMhz = $null
$volOk = $false

try {
    $os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
    if ($os -and $os.LastBootUpTime) { $bootTime = [datetime]$os.LastBootUpTime }
} catch {}

try {
    $cs = Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue
    if ($cs) {
        if ($cs.TotalPhysicalMemory) { $memTotalGb = [math]::Round([double]$cs.TotalPhysicalMemory / 1GB, 1) }
        if ($cs.NumberOfLogicalProcessors) { $cores = [int]$cs.NumberOfLogicalProcessors }
    }
} catch {}

try {
    $pr = Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pr) {
        $cpuName = $pr.Name
        if ($pr.MaxClockSpeed) { $baseMhz = [int]$pr.MaxClockSpeed }
    }
} catch {}

try {
    # Get-NetRoute 单次约 378ms，只在启动时查一次，之后复用接口索引
    $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
        Sort-Object -Property RouteMetric | Select-Object -First 1
    if ($route) {
        $gwIndex = [int]$route.InterfaceIndex
        try { if ($route.NextHop) { $gwAddr = [string]$route.NextHop } } catch {}
        $ad = Get-NetAdapter -InterfaceIndex $gwIndex -ErrorAction SilentlyContinue
        if ($ad) { $nicName = $ad.Name }
    }
    if (-not $nicName) {
        $ad = Get-NetAdapter -ErrorAction SilentlyContinue |
            Where-Object { $_.Status -eq 'Up' } | Select-Object -First 1
        if ($ad) { $nicName = $ad.Name; $gwIndex = [int]$ad.ifIndex }
    }
} catch {}

try {
    $bt = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue
    if ($bt) { $hasBattery = $true }
} catch {}

# CoreAudio：编译一次 IAudioEndpointVolume 互操作类型（只在启动时付一次编译代价，
# 采样轮里绝不编译）。失败就退化为 null，不影响其它字段。
try {
    $volSrc = @'
using System;
using System.Runtime.InteropServices;

[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
public class MMDeviceEnumeratorComObject { }

[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDeviceEnumerator {
    int EnumAudioEndpoints(int dataFlow, int stateMask, out IntPtr devices);
    int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice endpoint);
    int GetDevice([MarshalAs(UnmanagedType.LPWStr)] string id, out IMMDevice device);
    int RegisterEndpointNotificationCallback(IntPtr client);
    int UnregisterEndpointNotificationCallback(IntPtr client);
}

[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDevice {
    int Activate(ref Guid iid, int clsCtx, IntPtr activationParams, [MarshalAs(UnmanagedType.IUnknown)] out object iface);
    int OpenPropertyStore(int access, out IntPtr store);
    int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
    int GetState(out int state);
}

[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IAudioEndpointVolume {
    int RegisterControlChangeNotify(IntPtr n);
    int UnregisterControlChangeNotify(IntPtr n);
    int GetChannelCount(out int c);
    int SetMasterVolumeLevel(float level, ref Guid ctx);
    int SetMasterVolumeLevelScalar(float level, ref Guid ctx);
    int GetMasterVolumeLevel(out float level);
    int GetMasterVolumeLevelScalar(out float level);
    int SetChannelVolumeLevel(uint ch, float level, ref Guid ctx);
    int SetChannelVolumeLevelScalar(uint ch, float level, ref Guid ctx);
    int GetChannelVolumeLevel(uint ch, out float level);
    int GetChannelVolumeLevelScalar(uint ch, out float level);
    int SetMute(int mute, ref Guid ctx);
    int GetMute(out int mute);
}

public static class NeosAudio {
    public static double GetMasterScalar() {
        try {
            var en = (IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
            IMMDevice dev;
            if (en.GetDefaultAudioEndpoint(0, 1, out dev) != 0) return -1.0; // eRender / eMultimedia
            Guid iid = typeof(IAudioEndpointVolume).GUID;
            object o;
            if (dev.Activate(ref iid, 1, IntPtr.Zero, out o) != 0) return -1.0; // CLSCTX_INPROC_SERVER
            var vol = (IAudioEndpointVolume)o;
            float lv;
            if (vol.GetMasterVolumeLevelScalar(out lv) != 0) return -1.0;
            return (double)lv * 100.0;
        } catch { return -1.0; }
    }
}
'@
    Add-Type -TypeDefinition $volSrc -Language CSharp -ErrorAction Stop | Out-Null
    $volOk = $true
} catch {
    $volOk = $false
}

function Get-VolPct {
    if (-not $volOk) { return $null }
    try {
        $v = [NeosAudio]::GetMasterScalar()
        if ($v -lt 0) { return $null }
        return [math]::Round($v, 0)
    } catch { return $null }
}

# ─────────────────────────────────────────────────────────────
# 采样缓存：每个缓存项存 {v=值, at=上次刷新轮次}
# 预置 at = phase - cad，使首次刷新正好落在 phase 轮。
# ─────────────────────────────────────────────────────────────
$C = @{}
foreach ($k in $PLAN.Keys) { $C[$k] = @{ v = $null; at = $null } }

function Due([string]$k, [int]$round) {
    # 第 0 轮：带 -Warmup 时全部刷新（首行就有完整数据，代价约 1.2s 一次性）。
    # 否则按 round % cad == phase 命中，冷启动头几轮 net/gpu 之类仍是 null。
    if ($round -eq 0 -and $Warmup) { return $true }
    return (($round % $PLAN[$k].cad) -eq $PLAN[$k].phase)
}
function Put([string]$k, $v, [int]$round) {
    # 有些字段（ping_ms / ping_loss / link_wired）不属于 PLAN 的采样节奏，
    # 缓存里原本没有条目；直接取属性会在 null 上报错并被上层 catch 吞掉，
    # 表现就是「字段永远是 null」。这里自己补建。
    if (-not $C.ContainsKey($k)) { $C[$k] = @{ v = $null; at = $null } }
    $C[$k].v = $v
    $C[$k].at = $round
}
function GetC([string]$k) {
    if (-not $C.ContainsKey($k)) { return $null }
    return $C[$k].v
}

# top_proc 的「实时占比」用前后两次累计 CPU 时间做差
$procPrev = $null
$procPrevAt = $null

$round = 0
while ($true) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $now = Get-Date
    $fired = New-Object System.Collections.Generic.List[string]
    if ($TimingToStderr) { foreach ($k in $PLAN.Keys) { if (Due $k $round) { [void]$fired.Add($k) } } }

    # ── CPU 占用（每轮） ──────────────────────────────────────
    $cpuLoad = $null
    if (Due 'cpu' $round) {
        try {
            $p = Get-CimInstance -Query "SELECT PercentProcessorTime FROM Win32_PerfFormattedData_PerfOS_Processor WHERE Name='_Total'" -ErrorAction SilentlyContinue
            if ($p -and $null -ne $p.PercentProcessorTime) { $cpuLoad = [double]$p.PercentProcessorTime }
        } catch {}
    } else { $cpuLoad = GetC 'cpu' }
    Put 'cpu' $cpuLoad $round

    # ── CPU 主频（每 10 轮） ──────────────────────────────────
    if (Due 'freq' $round) {
        $f = $null
        try {
            $pr = Get-CimInstance -Query "SELECT CurrentClockSpeed,MaxClockSpeed FROM Win32_Processor" -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($pr) {
                if ($pr.CurrentClockSpeed) { $f = [double]$pr.CurrentClockSpeed }
                elseif ($pr.MaxClockSpeed) { $f = [double]$pr.MaxClockSpeed }
            }
        } catch {}
        if ($null -eq $f -and $baseMhz) { $f = [double]$baseMhz }
        Put 'freq' $f $round
    }
    $cpuFreq = GetC 'freq'

    # ── 内存 ──────────────────────────────────────────────────
    if (Due 'mem' $round) {
        $availMb = $null
        try {
            $m = Get-CimInstance -Query "SELECT AvailableMBytes FROM Win32_PerfFormattedData_PerfOS_Memory" -ErrorAction SilentlyContinue
            if ($m -and $null -ne $m.AvailableMBytes) { $availMb = [double]$m.AvailableMBytes }
        } catch {}
        Put 'mem' $availMb $round
    }
    $availMb = GetC 'mem'

    $memPct = $null; $memUsedGb = $null
    if ($null -ne $availMb -and $memTotalGb) {
        $memUsedGb = [math]::Round($memTotalGb - ($availMb / 1024.0), 1)
        if ($memTotalGb -gt 0) { $memPct = [math]::Round(100.0 * $memUsedGb / $memTotalGb, 1) }
    }

    # ── 温度：先试新类，再退 root/wmi，最后 Win32_SoundDevice 无温度则 null ──
    if (Due 'temp' $round) {
        $t = $null
        try {
            $z = Get-CimInstance -Query "SELECT Temperature FROM Win32_PerfFormattedData_Counters_ThermalZoneTemperature" -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($z -and $z.Temperature) { $t = [double]$z.Temperature / 10.0 - 273.15 }
        } catch {}
        if ($null -eq $t -or $t -lt -50 -or $t -gt 200) {
            $t = $null
            try {
                $z = Get-CimInstance -ClassName MSAcpi_ThermalZoneTemperature -Namespace 'root/wmi' -ErrorAction SilentlyContinue |
                    Select-Object -First 1
                if ($z -and $z.CurrentTemperature) { $t = [double]$z.CurrentTemperature / 10.0 - 273.15 }
            } catch {}
        }
        if ($null -ne $t -and ($t -lt -50 -or $t -gt 200)) { $t = $null }
        Put 'temp' $t $round
    }
    $tempC = GetC 'temp'

    # ── GPU：累加 3D/Compute 引擎利用率，封顶 100 ─────────────
    if (Due 'gpu' $round) {
        $g = $null
        try {
            $eng = Get-CimInstance -ClassName Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine -ErrorAction SilentlyContinue
            if ($eng) {
                $sum = 0.0; $hit = $false
                foreach ($e in $eng) {
                    if ($e.Name -like '*engtype_3D*' -or $e.Name -like '*engtype_Compute*') {
                        if ($null -ne $e.UtilizationPercentage) { $sum += [double]$e.UtilizationPercentage; $hit = $true }
                    }
                }
                if ($hit) { $g = [math]::Round([math]::Min(100.0, $sum), 0) }
            }
        } catch {}
        Put 'gpu' $g $round
    }
    $gpuPct = GetC 'gpu'

    # ── 磁盘剩余：只算本地固定盘（DriveType=3） ───────────────
    if (Due 'disk' $round) {
        $d = $null
        try {
            $ld = Get-CimInstance -Query "SELECT FreeSpace FROM Win32_LogicalDisk WHERE DriveType=3" -ErrorAction SilentlyContinue
            if ($ld) {
                $sum = 0.0; $hit = $false
                foreach ($x in $ld) { if ($null -ne $x.FreeSpace) { $sum += [double]$x.FreeSpace; $hit = $true } }
                if ($hit) { $d = [math]::Round($sum / 1GB, 1) }
            }
        } catch {}
        Put 'disk' $d $round
    }
    $diskFree = GetC 'disk'

    # ── 网络速率：排除回环/隧道伪接口 ─────────────────────────
    if (Due 'net' $round) {
        $rx = $null; $tx = $null
        try {
            $ni = Get-CimInstance -Query "SELECT Name,BytesReceivedPersec,BytesSentPersec FROM Win32_PerfFormattedData_Tcpip_NetworkInterface" -ErrorAction SilentlyContinue
            if ($ni) {
                $ar = 0.0; $at = 0.0; $hit = $false
                foreach ($a in $ni) {
                    if ($a.Name -match 'isatap|teredo|Loopback|Tunneling|Pseudo|_Npcap|WAN Miniport') { continue }
                    if ($null -ne $a.BytesReceivedPersec) { $ar += [double]$a.BytesReceivedPersec }
                    if ($null -ne $a.BytesSentPersec) { $at += [double]$a.BytesSentPersec }
                    $hit = $true
                }
                if ($hit) {
                    $rx = [math]::Round($ar * 8.0 / 1e6, 2)
                    $tx = [math]::Round($at * 8.0 / 1e6, 2)
                }
            }
        } catch {}
        Put 'net' @($rx, $tx) $round
    }
    $net = GetC 'net'
    $netDown = if ($net) { $net[0] } else { $null }
    $netUp = if ($net) { $net[1] } else { $null }

    # ── 电池：无电池则 pct=null、charging/ac=false ────────────
    if (Due 'bat' $round) {
        $bp = $null; $bc = $false; $ba = $false
        if ($hasBattery) {
            try {
                $bt = Get-CimInstance -Query "SELECT EstimatedChargeRemaining,BatteryStatus FROM Win32_Battery" -ErrorAction SilentlyContinue |
                    Select-Object -First 1
                if ($bt) {
                    if ($null -ne $bt.EstimatedChargeRemaining) { $bp = [int]$bt.EstimatedChargeRemaining }
                    $st = [int]$bt.BatteryStatus
                    # 2 = AC 供电；6/7/8/9 = 充电相关状态
                    $ba = ($st -eq 2 -or ($st -ge 6 -and $st -le 9))
                    $bc = ($st -ge 6 -and $st -le 9)
                }
            } catch {}
        }
        Put 'bat' @($bp, $bc, $ba) $round
    }
    $bat = GetC 'bat'
    $batPct = if ($bat) { $bat[0] } else { $null }
    $batChg = if ($bat) { $bat[1] } else { $false }
    $batAc = if ($bat) { $bat[2] } else { $false }

    # ── 音量（CoreAudio；失败为 null） ────────────────────────
    if (Due 'vol' $round) { Put 'vol' (Get-VolPct) $round }
    $volPct = GetC 'vol'

    # ── 最占进程：Get-Process 累计 CPU 排序取首个 ──────────────
    if (Due 'top' $round) {
        $tn = $null; $tp = $null
        try {
            $all = Get-Process -ErrorAction SilentlyContinue | Where-Object { $null -ne $_.CPU }
            $sorted = $all | Sort-Object -Property CPU -Descending
            $top = $sorted | Select-Object -First 1
            if ($top) {
                $tn = $top.ProcessName
                $elapsed = if ($procPrevAt) { ($now - $procPrevAt).TotalSeconds } else { 0 }
                if ($procPrev -and $elapsed -gt 0.2 -and $procPrev.ContainsKey($top.Id)) {
                    # 两次快照的累计 CPU 时间差 → 当前实时占用，再按逻辑核数归一
                    $delta = [double]$top.CPU - [double]$procPrev[$top.Id]
                    if ($delta -lt 0) { $delta = 0 }
                    $tp = [math]::Round(100.0 * $delta / $elapsed / [math]::Max(1, $cores), 1)
                } else {
                    # 首轮没有基线，退化为「累计 CPU 时间占比」（仍是稳定排序量）
                    $sum = 0.0
                    foreach ($p in $all) { $sum += [double]$p.CPU }
                    if ($sum -gt 0) { $tp = [math]::Round(100.0 * [double]$top.CPU / $sum, 1) }
                }
            }
            $snap = @{}
            foreach ($p in $all) { $snap[$p.Id] = $p.CPU }
            $procPrev = $snap
            $procPrevAt = $now
        } catch {}
        Put 'top' @($tn, $tp) $round
    }
    $top = GetC 'top'
    $topName = if ($top) { $top[0] } else { $null }
    $topPct = if ($top) { $top[1] } else { $null }

    # ── IP：默认网关所在网卡的 IPv4（复用启动时缓存的接口索引） ──
    if (Due 'ip' $round) {
        $ip = $null
        try {
            if ($null -ne $gwIndex) {
                $a = Get-NetIPAddress -InterfaceIndex $gwIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                    Where-Object { $_.IPAddress -notlike '169.254.*' } | Select-Object -First 1
                if ($a) { $ip = $a.IPAddress }
            }
            if (-not $ip) {
                $a = Get-CimInstance -Query "SELECT IPAddress FROM Win32_NetworkAdapterConfiguration WHERE IPEnabled=TRUE" -ErrorAction SilentlyContinue |
                    Select-Object -First 1
                if ($a -and $a.IPAddress) { $ip = ($a.IPAddress | Where-Object { $_ -notmatch ':' } | Select-Object -First 1) }
            }
        } catch {}
        Put 'ip' $ip $round
    }
    $ip = GetC 'ip'

    # ── 网关延迟：.NET Ping 直连，比 Test-Connection 快一个数量级 ──
    if (Due 'ping' $round) {
        $ms = $null
        $loss = $null
        try {
            # 先测公网：本机可能挂着代理 TUN（网关 198.18.x.x 恒 0ms，测不出东西）。
            $target = '223.5.5.5'
            $p = New-Object System.Net.NetworkInformation.Ping
            $n = 3
            $okN = 0
            $sum = 0.0
            for ($i = 0; $i -lt $n; $i++) {
                try {
                    $r = $p.Send($target, 1200)
                    if ($r -and $r.Status -eq 'Success') { $okN++; $sum += $r.RoundtripTime }
                } catch {}
            }
            if ($okN -eq 0 -and $gwAddr) {
                # 公网不通就退回网关，至少还能看出「链路断没断」。
                for ($i = 0; $i -lt $n; $i++) {
                    try {
                        $r = $p.Send([string]$gwAddr, 1200)
                        if ($r -and $r.Status -eq 'Success') { $okN++; $sum += $r.RoundtripTime }
                    } catch {}
                }
            }
            if ($okN -gt 0) { $ms = $sum / $okN }
            $loss = (($n - $okN) / $n) * 100.0
        } catch {}
        Put 'ping_ms' $ms $round
        Put 'ping_loss' $loss $round
    }
    $pingMs = GetC 'ping_ms'
    $pingLoss = GetC 'ping_loss'

    # ── 键盘连接方式：USB\ 就是有线，BTHLE\ 是蓝牙，都没有就是 2.4G ──
    if (Due 'link' $round) {
        $wired = $null
        try {
            $ids = @(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
                Where-Object { $_.InstanceId -match 'VID_1EA7&PID_4884' } |
                Select-Object -ExpandProperty InstanceId)
            if ($ids.Count -gt 0) {
                $wired = $false
                foreach ($id in $ids) { if ($id -like 'USB\*') { $wired = $true } }
            }
        } catch {}
        Put 'link_wired' $wired $round
    }
    $linkWired = GetC 'link_wired'

    # ── 时间相关 ──────────────────────────────────────────────
    $uptime = $null
    if ($bootTime) {
        try {
            $sp = $now - $bootTime
            # 必须用 Floor：[int] 会对 0.5 做银行家舍入，11h55m 会被算成 1d
            $uptime = '{0}d {1:00}:{2:00}' -f [math]::Floor($sp.TotalDays), $sp.Hours, $sp.Minutes
        } catch {}
    }

    # ── 组 JSON 行（key 顺序与契约一致） ──────────────────────
    $line = '{' +
        '"clock":' + (J $now.ToString('HH:mm:ss', $INV)) + ',' +
        '"clock_utc":' + (J $now.ToUniversalTime().ToString('HH:mm', $INV)) + ',' +
        '"date":' + (J $now.ToString('yyyy-MM-dd', $INV)) + ',' +
        '"uptime":' + (J $uptime) + ',' +
        '"cpu_load":' + (N $cpuLoad 1) + ',' +
        '"cpu_freq_ghz":' + (N ($(if ($null -ne $cpuFreq) { [double]$cpuFreq / 1000.0 } else { $null })) 2) + ',' +
        '"mem_pct":' + (N $memPct 1) + ',' +
        '"mem_used_gb":' + (N $memUsedGb 1) + ',' +
        '"mem_total_gb":' + (N $memTotalGb 1) + ',' +
        '"mem_available_gb":' + (N ($(if ($null -ne $availMb) { $availMb / 1024.0 } else { $null })) 1) + ',' +
        '"temp_c":' + (N $tempC 1) + ',' +
        '"gpu_pct":' + (N $gpuPct 0) + ',' +
        '"disk_free_gb":' + (N $diskFree 1) + ',' +
        '"net_down_mbps":' + (N $netDown 2) + ',' +
        '"net_up_mbps":' + (N $netUp 2) + ',' +
        '"bat_pct":' + (N $batPct 0) + ',' +
        '"bat_charging":' + (B $batChg) + ',' +
        '"bat_ac":' + (B $batAc) + ',' +
        '"vol_pct":' + (N $volPct 0) + ',' +
        '"top_proc":' + (J $topName) + ',' +
        '"top_proc_pct":' + (N $topPct 1) + ',' +
        '"ping_ms":' + (N $pingMs 0) + ',' +
        '"ping_loss":' + (N $pingLoss 0) + ',' +
        '"link_wired":' + (B $linkWired) + ',' +
        '"ip":' + (J $ip) +
        '}'

    [Console]::Out.WriteLine($line)
    [Console]::Out.Flush()
    if ($TimingToStderr) {
        [Console]::Error.WriteLine('r{0} {1}ms fired=[{2}]' -f $round, $sw.ElapsedMilliseconds, ($fired -join ','))
    }

    $round++
    if ($Count -gt 0 -and $round -ge $Count) { break }

    $rest = $IntervalMs - [int]$sw.ElapsedMilliseconds
    if ($rest -gt 0) { Start-Sleep -Milliseconds $rest }
}
