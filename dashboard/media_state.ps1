# dashboard/media_state.ps1 —— 常驻读取「正在播放」，每行输出一个 JSON 对象。
#
# 为什么单独一个进程、且固定用 Windows PowerShell 5.1：
#   SMTC（Windows.Media.Control）是 WinRT 组件，只有 5.1 带 WinRT 投影；
#   PowerShell 7（pwsh）里 [Windows.Media.Control.*] 这个类型根本不存在。
#   而主采样器 live_state.ps1 优先用 pwsh（更快），所以音乐单独起一路。
#
# 实测单次查询 4~5 ms（首次 RequestAsync 约 46 ms），2 秒一轮几乎无开销。
#
# 输出字段（缺数据一律 null，不省略）：
#   app title artist album status position duration
# status: playing / paused / stopped / none

param(
    [int]$IntervalMs = 2000,
    [int]$Count = 0
)

$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'

try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch {}
try { $OutputEncoding = [Console]::OutputEncoding } catch {}

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

function Num([object]$v, [int]$d) {
    if ($null -eq $v) { return 'null' }
    try {
        $x = [double]$v
        if ([double]::IsNaN($x) -or [double]::IsInfinity($x)) { return 'null' }
        if ($d -le 0) { return ([math]::Round($x, 0)).ToString('0') }
        return ([math]::Round($x, $d)).ToString('0.' + ('0' * $d))
    } catch { return 'null' }
}

# ── 一次性初始化（WinRT 反射拿 AsTask，约 350 ms，只付一次） ──
$ready = $false
$asTask = $null
$mgrType = $null
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime -ErrorAction Stop | Out-Null
    $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]
    $mgrType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime]
    $ready = ($null -ne $asTask -and $null -ne $mgrType)
} catch {
    $ready = $false
}

function Await($op, $t, [int]$waitMs) {
    $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op))
    if (-not $task.Wait($waitMs)) { return $null }
    return $task.Result
}

function ReadOnce() {
    if (-not $ready) {
        return '{' + '"app":null,"title":null,"artist":null,"album":null,' +
            '"status":"none","position":null,"duration":null}'
    }
    $app = $null; $title = $null; $artist = $null; $album = $null
    $status = 'none'; $pos = $null; $dur = $null
    try {
        $mgr = Await ($mgrType::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]) 1500
        if ($null -ne $mgr) {
            $cur = $mgr.GetCurrentSession()
            if ($null -ne $cur) {
                $app = $cur.SourceAppUserModelId
                $info = $cur.GetPlaybackInfo()
                if ($null -ne $info -and $info.PlaybackStatus) {
                    $status = ([string]$info.PlaybackStatus).ToLowerInvariant()
                }
                $props = Await ($cur.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties]) 1500
                if ($null -ne $props) {
                    $title = $props.Title
                    $artist = $props.Artist
                    $album = $props.AlbumTitle
                }
                $tl = $cur.GetTimelineProperties()
                if ($null -ne $tl) {
                    $pos = $tl.Position.TotalSeconds
                    $dur = $tl.EndTime.TotalSeconds
                    if ($dur -le 0) { $dur = $null }
                }
            }
        }
    } catch { }
    return '{' + '"app":' + (J $app) + ',"title":' + (J $title) + ',"artist":' + (J $artist) +
        ',"album":' + (J $album) + ',"status":' + (J $status) + ',"position":' + (Num $pos 1) +
        ',"duration":' + (Num $dur 1) + '}'
}

$round = 0
while ($true) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    [Console]::Out.WriteLine((ReadOnce))
    [Console]::Out.Flush()
    $round++
    if ($Count -gt 0 -and $round -ge $Count) { break }
    $rest = $IntervalMs - [int]$sw.ElapsedMilliseconds
    if ($rest -gt 0) { Start-Sleep -Milliseconds $rest }
}
