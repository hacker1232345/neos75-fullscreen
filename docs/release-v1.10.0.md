# v1.10.0 — 屏幕能看的东西多了

这一版没有改固件。键盘上跑的还是你自己那份，改动全在电脑这边：屏幕上能显示的内容
从「只有数字」变成「也能看歌、看日子、看链路」。

## 新增四个组件

| 组件 | 显示什么 | 数据从哪来 |
|---|---|---|
| 正在播放 `playing` | 歌名、歌手、播放/暂停、进度条 | Windows SMTC |
| 日历 `calendar` | 今天几号、星期、本年第几周 | 系统时间 |
| 连接 `link` | 有线 USB / 2.4G、往返延迟、丢包 | ping + PnP 实例 ID |
| 网速条 `net_pulse` | 上下行两条长度条 | 网卡计数 |

## 两个新预设

- `desk`：时间 + 正在播放 + 日历 + CPU/内存 + 连接 + 网速条 + 电池，八个组件一屏看完。
- `media`：听歌用，歌名占半屏。

```sh
node dashboard/push.js once --preset desk
node dashboard/push.js once --preset media
```

桌面程序的预设下拉里也有这两项，选完直接看预览。

## 为什么「正在播放」必须用 PowerShell 5.1

`Windows.Media.Control` 是 WinRT 组件。PowerShell 7 没有 WinRT 投影，
`[Windows.Media.Control.GlobalSystemMediaTransportStatesSessionManager]`
这种写法会直接报「找不到类型」——我第一次就是这么撞墙的。所以 `media_state.ps1`
固定用 `powershell.exe`（5.1），并且用反射取 `AsTask` 的重载来同步等待异步调用。

初始化约 350 ms，之后每轮 4–5 ms。2 秒采一次，对帧率没有可测量的影响。

## 延迟为什么测公网

这台机器的默认网关是 `198.18.0.2`——代理 TUN 的虚拟网关，ping 恒 0–1 ms。
放在屏幕上等于没信息。改成测 `223.5.5.5`，只有完全不通时才退回网关，
那时至少还能看出「链路断了」。

拿不到延时写 `n/a`，不写 `0`。这条有测试护着：把 `n/a` 改成 `0`，断言立刻失败。

## 踩平的两个坑

1. **日历显示成两个横杠。** `2026-11-06 周五` 整串不是数字，而原代码是逐段找
   `isdigit()` 的短数字，于是全部落空，屏幕上就是 `--`。改成按 `-` 切开取第三段。
2. **`Put` 往缓存里写不存在的键。** `ping_ms` / `ping_loss` / `link_wired` 不在
   采样节奏表里，`$C[$k].v = ...` 在 `$null` 上取属性会抛错，又被 `catch {}` 吞掉，
   表现是这三个字段永远 `null` / `false`。`Put` 现在自己补建条目。

## 怎么验的

- 162 项 Python 测试 + 12 组 node 测试全过。
- `--self-test` 11 项 PASS：`21 widgets / presets codex,default,desk,full,media,minimal,network,performance,weather`。
- 真机推送：`push.js once --preset desk` → 371 帧、6 个应答、单轮 9.4 秒。
- 反向验证两条断言都能抓住回归（改坏实现 → 测试红 → 恢复 → 绿）。

## 仍然没解决的

- 温度 `temp_c` 在本机恒为 `null`（两个 WMI 热区接口都取不到），屏幕上照实写 `n/a`。
- 「正在播放」只覆盖走 SMTC 的播放器。独占 WASAPI 的程序（少数游戏、部分播放器）看不到。
