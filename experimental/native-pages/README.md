# 原生第 6 页实验（尚未真机验收）

目标：**第 5 页保留图片/GIF，第 6 页变成独立的电脑信息页，用键盘原有翻页切换。**

本目录是独立研究原型，未加入 v1.4.0 桌面程序、稳定补丁配置或一键刷写流程。没有自动刷写工具。仅接受代码 SHA-256 为 `8465dd6334eb0dc0d0862477b5df05c5d957b1bfe3d19ba1ad222fd3397ef4b7` 的完整 491520 字节 reference 全屏＋清爽 payload。其他构建（包括 Issue #1）拒绝，不能按版本号或设备号套用。

原始备份和实验固件均留在本机，不提交含现场配置的二进制。完整逆向记录见 [数据存储与绘制研究](../../docs/native-pages-research.md)。

## 当前能力

- 第 6 页独立创建 480×360 背景、4 个文字对象和 250 ms 绘制定时器，替代原第 6 页功能。
- 自定义 `0x3D` 命令仅更新 RAM；电脑不走 JPEG/GIF 上传和存储写入路径。
- 在其他页面接收数据，切回第 6 页才更新文字，不由推流程序强制翻页。
- 15 秒无新数据后显示 `PC disconnected`，其他行清成 `--`。重新发送后恢复。
- 仅支持每行 0–47 字节可打印 ASCII。天气、Codex 状态转成英文，温度/电量不可读时显示 N/A。
- 组件数据断电不保存。首次启动需手动切到第 6 页，完成对象初始化后才接收更新。

这里的“保留图片/GIF”表示其代码路径和页面表未改；实际切换、播放、显示效果仍待设备验证。六个原厂页面也不等于六个独立图片存储槽。

## 离线复现

以下命令在仓库根目录执行。`reference-clean-payload.bin` 由自己的已知构建完整备份经稳定补丁链生成，并移除封装末尾的 16 字节 DFU suffix；不要截断现场 payload 或用别人的配置替代自己的备份。

```powershell
python -m pip install -r experimental/native-pages/requirements.txt
npm ci
python experimental/native-pages/inspect_firmware.py --input reference-clean-payload.bin
python experimental/native-pages/inspect_firmware.py --input reference-clean-payload.bin --disassemble 0x3c128 0x3c258
python experimental/native-pages/test_emulation.py --input reference-clean-payload.bin -v
node experimental/native-pages/test_send.js
python experimental/native-pages/test_stream.py -v
python experimental/native-pages/build_native.py --input reference-clean-payload.bin --out ../native-lab/page6.bin
```

builder 只生成文件与审计 JSON，不导入 USB/DFU 库，不覆盖已有输出，没有 force 选项。末尾 4096 字节配置逐字节保留，输出补全 DFU suffix 与 CRC。它不代表可以绕过稳定程序的固件验证。

## 电脑端文字预览

需要桌面程序现有 Python 依赖（psutil、Pillow，以及系统的 Tk 支持）。预览不打开 USB；若配置了城市，天气适配器可能请求 Open-Meteo。只读桌面程序的 `settings.json`，不改设置或安装 Codex hooks。

```powershell
python experimental/native-pages/stream.py --preview
```

也可用 `--settings 文件.json` 指定只读配置，例如：

```json
{"weather":{"label":"Shanghai","latitude":31.23,"longitude":121.47},"codex_thread":""}
```

城市必须自行确认；留空时显示 `WX SET CITY ON PC`。Codex 读取现有本地事件，只显示状态和事件年龄，不推测完成百分比、不发送对话内容。温度传感器缺失时显示 N/A。

## 后续真机验证时的推流

**先独立验收候选固件与恢复路径，再执行以下命令。当前稳定版没有该接收器。** 先退出官方驱动和其他推流程序，确保仅一个程序占用接口；无线链路未验证。

```powershell
node experimental/native-pages/send.js --probe
python experimental/native-pages/stream.py --send --yes --interval 5
# 可先只发一次：
python experimental/native-pages/stream.py --send --yes --count 1
```

`--probe` 只查询，不自动创建页面。原匹配 reference 固件回复 FC；已打实验补丁但尚未切入第 6 页回复 FD；初始化后回复 FE。发送器先确认能力，随后逐行等待 ACK；拒绝或超时即停止，不重发，不改固件、不上传媒体。四行不是一个原子事务，整组时间显示在最后一行。

亦可提供一个恰好含 4 个字符串的 JSON 数组：

```powershell
node experimental/native-pages/send.js --rows rows.json --yes
```

## 验证边界

Unicorn 实际执行新 Thumb 指令，以及原厂的启动标志初始化、A5 解析器、校验和、应答封装、页面表切换和通用更新函数。LVGL、分配器、定时器创建、事件分发、HID、LCD、RTOS 和真实堆余量未完整模拟；相关函数使用明确的测试桩。仿真通过不能代替上电、休眠唤醒、长时间运行和图片/GIF来回切换验收。

不应通过稳定程序的“无验证”功能尝试强刷本候选。该模式仍面向已定义全屏补丁，不是本实验的安装入口。
