# Neos75 全屏工具 v1.2.0

本版在原有全屏补丁之上加了**两层东西**：一层是可叠加的固件补丁，一层是可选的屏幕部件。

## 补丁链

固件配置从「一份补丁」变成「一条链」。每层用代码区SHA256精确衔接：上游的 `target_code_sha256` 必须等于下游的 `source_code_sha256`，不做「看起来像就套用」的推断。

| 层 | 配置 | 从 → 到 | 改动 | 验证状态 |
| --- | --- | --- | --- | --- |
| 第一层 | `firmware/fullscreen.patch.json` | `66679ff2` → `cdd6334d` | 27 字节 | 实机验收 |
| 第二层 | `firmware/profiles/clean.patch.json` | `cdd6334d` → `8465dd63` | 13 字节 | 实机刷入并逐字节回验 |
| 另一条链 | `firmware/profiles/issue1.patch.json` | `74f2fa47` → `9093f898` | 27 字节 | 离线通过，待实机验收 |

程序识别三种状态：

- **链首**（`supported-original`）：原版备份。生成时自动连打全部剩余层，界面列出「将依次应用：原验证构建 → 全屏+清爽增强」。
- **链中**（`upgradeable`）：已经是第一层。生成时**只补剩余那层**，不重复打已有补丁。
- **链末**（`already-fullscreen`）：已经是最新。拒绝重复刷写。

逐层应用与一次性应用的结果**逐字节一致**，这是有断言保证的（两层改动的字节集交集必须为空）。

## 全屏+清爽增强（第二层）

这一层只改13个字节，解决三件事：

| 地址 | 文件偏移 | 旧 → 新 | 作用 |
| --- | --- | --- | --- |
| `0x3453c` | `0x2c53c` | `f2f72cf9` → `00bf00bf` | 上传开始时不再隐藏全屏图片对象，消除上传期间满屏黑 |
| `0x345aa` | `0x2c5aa` | `09f06bff` → `00bf00bf` | 跳过进度环绘制调用 |
| `0x345cf` | `0x2c5cf` | `f6b831` → `f2dc51` | 上传收尾阈值 3000ms → 1500ms |
| `0x3e54a` | `0x3654a` | `ff` → `00` | 覆盖层容器初始化即隐藏 |
| `0x3e69c` | `0x3669c` | `30` → `00` | 覆盖层标签初值串 `"0%\n"` 清空 |

效果：满屏绿环、中央常驻 `0%` 全部消失；上传期间旧画面保持可见，新帧解码完自然替换；连续推送从约33秒一换变成3~4秒一换。已在本机实机刷入并逐字节回验一致。

第二层触及两个4KB扇区 `0x34000`、`0x3e000`（8KB组口径相同）。

## 屏幕部件（可选显示）

`dashboard/` 把电脑状态画成 480×360 baseline JPEG 推给屏幕。15个部件分六组：

| 分组 | 部件 |
| --- | --- |
| 时间 | `clock_big` 超大时钟、`date` 日期 |
| 性能 | `cpu`、`mem`、`gpu`、`temp` 温度、`history_cpu` 折线、`top_proc` 最占进程 |
| 存储 | `disk` 磁盘剩余 |
| 网络 | `net` 上下行速率、`ip` 地址 |
| 系统 | `host` 主机名、`uptime` 运行时长 |
| 电源 | `battery` 电池、`volume` 音量 |

预设 `minimal` / `default` / `full` / `performance` / `network`，也可任意勾选。默认预设 = 时钟、日期、CPU、内存、温度、磁盘、网络、电池、运行时长。

```sh
python dashboard/render.py --list
node   dashboard/push.js info
node   dashboard/push.js dry --preset full
node   dashboard/push.js once --widgets clock_big,cpu,mem,net
node   dashboard/push.js loop --preset default --interval 3000
```

桌面程序新增第4页「屏幕部件」：勾选部件 → 采样并预览 → 推送一帧 / 连续推送。采样与渲染完全不碰USB，只有推送才连接键盘。

## 已知限制

- **温度在本机读不到**：`Win32_PerfFormattedData_Counters_ThermalZoneTemperature` 与 `root/wmi MSAcpi_ThermalZoneTemperature` 均无数据，`temp_c` 恒为 `null`，界面上显示 `n/a`。不填假数据。
- **推送后没有最终ACK**：原厂协议在传输结束后不回报最终结果，`displayVerified` 恒为 `false`。程序提示看屏幕确认，不能由命令成功推断显示成功。
- **采样下限约1200ms**：稳态一次采样约0.62秒，`--interval` 低于约1200ms没有意义。
- **同一时刻只能有一个上传者**：连续推送时要先关掉官方程序的上传任务。
- **`full` 预设下 CPU/内存大数字顶部会被裁**：7行布局缩放后这两张卡片的高度不足。当前未修，用 `default` 或 `performance` 预设不受影响。

## 验证

`npm test` 6个套件全部通过（传输、备份、壁纸协议23组、强制刷写模型、补丁链、屏幕部件13组）。`python -m unittest discover -s tests -v` 全部通过。`--self-test` 10项通过（新增 `board_selftest`、`board_registry`），`--gui-smoke` 通过并新增部件注册表与预设断言。全部使用合成数据，不包含设备转储。

复盘与五列风险表见 [屏幕部件复盘](dashboard-retrospective.md) 与 [项目复盘](retrospective.md)。
