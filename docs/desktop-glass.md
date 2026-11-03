# Neos75 桌面新版（v1.3.0 开发版）

全屏补丁与键盘小组件放在同一个程序中。默认界面只保留这两个页面。

## 一键全屏

用 USB 数据线直连键盘，关闭官方程序及其他上传工具。点“一键全屏”，确认后等待完成，再到官方程序重新导入图片或 GIF。

程序自动读取这把键盘的完整固件，每块双读比对并落盘校验，再按代码哈希选择补丁链，生成目标并刷入。每个写入保护组都逐字节回验，成功后请求退出 DFU。未知构建默认停止；已到补丁链末端则不重复刷写。兼容范围沿用 v1.2.0，不因界面改造扩大。

不需要事先持有原始备份。备份在 Windows 的 `%LOCALAPPDATA%\Neos75\backups\<本次ID>\`，或 Mac 的 `~/Library/Application Support/Neos75/backups/<本次ID>/`。

“备份与恢复”可以打开目录或恢复最近一次操作前的状态。恢复前验证保存的目标、重新读取现场固件，并检查补丁保护组以外的字节是否与备份一致；不匹配就停止。恢复时须连接同一把键盘。生成目标与恢复记录保存在任何擦写之前。

高级选项保留“无验证，直接打补丁”，每次启动默认关闭且不保存选择。它跳过版本哈希与旧字节检查，不能证明未知版本兼容；自动备份、目标约束和写后回验仍保留。

## 自由添加小组件

组件库包含时钟、日期、CPU、内存、GPU、温度、CPU 历史、进程、网络速率、IP、主机名、磁盘、电池、音量和运行时长。可搜索、添加、移除、上下排序，或套用日常、性能、网络等预设。配置自动保存。

按顺序自动分页，每页最多 6 项，不压缩到低于组件声明的最小高度。预览与实际发送使用同一份 480×360 baseline JPEG；全部 15 项当前分 3 页。点“开始同步”并确认后才会上传，按 3 / 5 / 10 秒目标间隔依次发送各页，实际间隔受 USB 速度限制。停止同步会等待当前图片发送结束。退出窗口也不会直接中断正在发送或刷写的操作。

Windows 复用原有 PowerShell 传感器采样，psutil 提供跨平台基础数据。温度、GPU、音量等受数据源限制，缺失时显示 n/a。最占进程沿用原采样器的累计 CPU 时间口径。Mac 的基础采样已接入，但新界面与 USB/DFU 仍需 Mac 真机验收。

传输结束只能证明主机完成发送，原厂协议尾段没有显示确认。界面提示“已发送，请在键盘屏幕确认显示”。无线传输尚未实现。

## 界面与运行环境

导航与控件参考 Apple Materials 的玻璃层次，正文保持浅色底。这是本地 WebView + CSS 外观，并非 Apple 原生 Liquid Glass 引擎。支持键盘焦点、对话框、减少动态效果与窄窗口排版。

Windows 需要 Microsoft Edge WebView2 Runtime（通常已随 Windows 安装）。程序自带 Python、Node、渲染器及依赖，用户不必安装开发环境。当前可执行包未做代码签名。

主界面只显示简短状态；完整操作日志收在“诊断”，并追加到用户数据目录 `diagnostics.log`。记录可能包含本机路径，公开提交前请自行检查。

## 开发与离线验收

```sh
npm ci --ignore-scripts
python -m pip install -r requirements-dev.txt pyinstaller==6.22.2
python desktop/app.py
python desktop/app.py --gui-smoke --result-file build/gui.json
python -m unittest discover -s tests -v
npm test
python desktop/build_exe.py
python desktop/ci_smoke.py
```

浏览器验收：`python desktop/preview.py`，访问输出的 127.0.0.1 地址。此工具仅有界面、配置与预览端点，固件与 USB 操作禁用，配置在临时目录，不随桌面入口运行。

三平台构建继续由 `.github/workflows/desktop.yml` 负责。新版本发布前需 Windows、macOS arm64、macOS x64 CI 通过。本地 Windows 构建不能代替 Mac 真机测试。
