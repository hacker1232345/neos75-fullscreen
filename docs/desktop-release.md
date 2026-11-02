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


## v1.1.2 强制模式

“设备与补丁”和“固件维护”页均提供“无验证，直接打补丁”，共用一次会话内的选择，默认不勾选。它跳过版本哈希及补丁旧字节检查，按固定偏移写入，不保证未知固件兼容。

选择完整原始备份，勾选后生成新文件；刷写/恢复时保持同一选项状态。原始备份不能被输出覆盖，目标必须与该备份按同一模式生成的结果完全一致。现场扇区检查、写后回验及恢复仍保留。命令行生成和维护均使用 `--force`，刷写/恢复仍须 `--yes`。


## v1.1.3 自动适配已知构建

选择备份或完成设备备份后，界面自动显示匹配的构建和验证状态。原验证构建、Issue #1构建均不需要勾选无验证选项，直接生成并使用同一原始备份进行预检、刷写与恢复。Issue #1目前为离线适配，待真实图片/GIF验收；未知构建默认拒绝，强制模式不具备搜索新地址的功能。


## v1.2.0 补丁链与屏幕部件

固件配置变成可叠加的链。原验证构建的备份会自动连打两层（全屏 → 全屏+清爽）；已经在中间态的设备识别为「可继续升级」，生成时**只补剩余那一层**。链末识别为「已是链末」，拒绝重复刷写。层级由代码区SHA256精确衔接，`parent` 只用于展示，不做推断。

新增第4页「屏幕部件」：勾选部件 → 采样电脑状态 → 480×360预览 → 推送一帧 / 连续推送。采样走 PowerShell（全CIM），渲染走 Pillow，两者完全不碰USB；只有推送才连接键盘。15个部件、5种预设，可任意组合。

需要注意的界面行为：

- 温度在本机读不到（两个热区WMI数据源均无数据），显示 `n/a`，不填假数据。
- 推送结束后原厂协议不回报最终结果，界面提示自行看屏确认，不能由命令成功推断显示成功。
- 连续推送是独立子进程，「停止连续推送」会终止它；同一时刻只允许一个上传者，推送前请关掉官方程序的上传任务。
- `full` 预设下 CPU/内存大数字顶部会被裁切，用 `default` 或 `performance` 不受影响。

`--self-test` 新增 `board_selftest`（13组）与 `board_registry` 两项；`--gui-smoke` 新增部件注册表、预设与默认勾选的断言。全部离线，不打开USB。

### 打包版为什么带一个渲染器

屏幕部件的采样、渲染、推送分别在 PowerShell、Python(Pillow)、Node 里，而打包出来的程序**不能假设目标机器装了 Python 和 Pillow**。所以构建时会把 `dashboard/render.py` 单独冻结成一个可执行文件 `Neos75-Render`（内含解释器和 Pillow），放进 `runtime/`：

```
dist/Neos75-Fullscreen.exe        主程序
   └─ runtime/Neos75-Render.exe   随包渲染器（就是 dashboard/render.py）
```

主程序通过环境变量 `NEOS75_RENDER` 把它交给 `dashboard/push.js` 和 `dashboard/selftest.js`，两者看到该变量就直接调 exe（命令行与脚本版完全一致），否则退回「python + 脚本」。因此**源码运行和打包运行走的是同一份 render.py**，不存在两套渲染逻辑。`desktop/dash_bridge.py` 的 `_render_command()` 与 `_renderer()` 就是这条分叉点，`tests/test_dashboard.py` 的 `RendererCommand` 类把两种模式都钉住了。

`render.py` 的 `_here()` 负责在冻结后找到 `widgets.json`：冻结时 `__file__` 指向临时解包目录，注册表跟着数据文件在 `sys._MEIPASS` 下，因此回退顺序是 `_MEIPASS/dashboard` → `_MEIPASS` → 脚本同目录。
