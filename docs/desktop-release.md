# 可执行程序：使用、构建与复盘

## 无需原始备份的起步流程

1. USB直连键盘，打开程序，点击“查询屏幕”。
2. 点击“从设备读取备份”，选择一个新文件名并确认进入DFU。
3. 等待64字节块双读校验、完整落盘及SHA256校验。读取不擦写Flash；完成后尝试回到应用。
4. 匹配原版会自动填入备份路径，接着点“生成全屏固件”。若显示已经全屏则无需重复升级；未知版本仅保存备份，停止生成与刷写。
5. 确需刷写时，进入“固件维护”，先预检，再刷写并回读；所有组通过后退出DFU。

无法进入DFU时需要检查直连模式与USB驱动，不能靠选择其他设备或跳过版本校验继续。断线或短读留下的 `.partial` 不是完整备份，程序不会把它自动选为可用备份。请保存备份旁的JSON校验记录。

## 平台与验证

Windows x64为单文件EXE；macOS 15+按Apple芯片和Intel分别构建 `.app` 并打包ZIP。均内置Python、Node和对应平台USB/HID原生模块，运行无需另装开发环境。macOS USB/DFU尚需在用户的Mac及键盘组合上验收，不能由构建通过推断硬件通过。

维护写入通路沿用已实机验证的MCU算法，窗口版仅增加调用入口与外部备份路径。启动不读取设备；操作期间禁用其他操作，关闭窗口会提示等待结束。查询/备份/维护区分显示，生成目标不会覆盖原始备份。

## 从源码构建

在目标操作系统、目标架构机器安装Node.js 24、Python 3.11和PyInstaller 6.22.2：

```sh
npm ci --ignore-scripts
python -m pip install pyinstaller==6.22.2
npm test
python -m unittest discover -s tests -v
python desktop/build_exe.py
python desktop/ci_smoke.py
```

Windows输出 `dist/Neos75-Fullscreen.exe`；Mac输出 `dist/Neos75-Fullscreen.app`。GitHub Actions `.github/workflows/desktop.yml` 在三种本机架构分别构建和检查；资源按显式目录允许清单打包，排除完整固件、私有备份、回滚文件和开发缓存。

打包规则参考 [PyInstaller运行时资源路径](https://pyinstaller.org/en/stable/runtime-information.html) 与 [spec文件](https://pyinstaller.org/en/stable/spec-files.html)；Mac构建机架构按 [GitHub标准运行器](https://docs.github.com/en/actions/reference/runners/github-hosted-runners) 选择。

可执行程序也支持 `--self-test`、`--gui-smoke`、`--status`、`--backup --out 新备份.bin --yes`、`--build 备份.bin --out 目标.bin`，配合 `--result-file 结果.json` 保存结果。命令行备份同样进入DFU、不擦写Flash。维护命令见 `desktop/app.py --help`，闪存写入要求 `--yes`。

## 复盘

初版窗口只允许选择已有原始备份，漏掉了普通用户没有备份的入口。用户指出后补上独立的设备备份模块，无需先拥有备份即可读取；短读、双读不一致、落盘校验失败和文件覆盖均拒绝。随后实测发现设备末尾4KB会在运行后改变，整片哈希会误拒同一代码构建。改为严格校验前487424字节代码区域及所有原字节，末尾4KB完整保留；Python和Node独立生成结果逐字节一致，目标必须等于本机备份的重新生成结果。未知代码版本仍拒绝。

独立运行验证会清空PATH，排除误用电脑已有Python/Node；默认离线测试使用USB模拟设备和协议参考函数。Windows实机只做查询与读取，不为窗口验收重复刷写。Mac构建及启动证据与硬件验收分别记录。

## 后续风险（按后果×概率排序）

| 风险点 | 触发条件 | 后果 | 前兆 | 对策 |
|---|---|---|---|---|
| 刷写中断或错版本 | 写入断电、跳过版本门控 | 屏幕无法启动 | 回验不符、DFU忙 | 严格SHA256、明确确认、逐块定位及回验、保留本机备份 |
| Mac硬件通路未验收 | 首次连接Mac与键盘 | 无法查询或进入DFU | 接口不可见、权限/传输错误 | 先查询和备份；报错保留日志，不执行擦写 |
| 备份不完整 | 读取中断、返回短数据或不一致 | 无法可靠回滚 | `.partial`、双读或SHA256失败 | 拒绝作为完整备份，不覆盖旧备份；修复连接后用新文件名重试 |
| 固件版本尚未适配 | 其他构建或个性化数据不同 | 不能生成全屏补丁 | 备份完整但SHA256未知 | 保存备份与校验记录，重新分析；不强行刷入当前目标 |
| 未签名或未公证 | 系统首次打开程序 | 系统提示或阻止启动 | 来源无法验证 | 提供源码、构建记录与SHA256；后续可用开发者证书签名公证 |
| 全屏内存余量 | 复杂或长GIF | 卡顿、首帧不出 | 动画停住或黑屏 | 已验证短GIF，复杂素材继续观察，必要时优化内存 |
