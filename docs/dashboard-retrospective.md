# v1.2.0 屏幕部件与补丁链复盘

## 交付结果

两件事合并进同一个程序：

1. **补丁链**：把「全屏+清爽」这层13字节的改动，以第二层补丁的形式挂在已发布的全屏补丁之上。原版备份自动连打两层；已经是全屏版的设备只补剩余那一层。
2. **屏幕部件**：新增 `dashboard/` 模块与桌面程序第4页，可在屏幕上显示15种电脑状态部件。

原有全屏功能、Issue #1 链、强制模式、备份与维护流程全部保持行为不变。

## 发布结果

`v1.2.0` 已发布：<https://github.com/hacker1232345/neos75-fullscreen/releases/tag/v1.2.0>

| 平台 | 产物 | 大小 |
| --- | --- | --- |
| Windows x64 | `Neos75-Windows-x64.exe` | 60.66 MB |
| macOS Apple 芯片 | `Neos75-macOS-arm64.zip` | 60.68 MB |
| macOS Intel | `Neos75-macOS-x64.zip` | 63.51 MB |

每个产物都附 `.sha256` 与 `.build.json`（后者含版本、平台、字节数与 `sha256`、构建时的 node 与 pyinstaller 版本）。

CI 三个平台的 job 全部 success，`release` job 也 success。第一次打 tag（`c9cabb3`）时 CI 三个平台在同一步全挂、`release` 被 skip，所以**那次等于什么都没发出去**；根因与修复见下面「绕了远路的地方」。

## 做对的地方

**先证明自己的改动挂在哪一层，再决定怎么合并。** 一开始我准备把它当「另一份固件」处理，但设备实读基准前487424字节的SHA256 = `cdd6334d…`，正好等于仓库 `fullscreen.patch.json` 的 `target_code_sha256`。这一条哈希把「新固件」变成「第二层补丁」，整个合并方案随之确定：不是加一个并列的 profile，而是让 profile 支持父子衔接、支持只补剩余层。

**链式衔接只认哈希，不认 parent 字段。** `parent` 只用于展示顺序和「是否已到最新」的判断；能不能应用一律由 `source_code_sha256`/`target_code_sha256` 决定。`issue1.patch.json` 压根没有 `parent` 字段，却仍能被正确识别为一条独立链的链首 —— 这就是解耦的好处。

**「逐层应用 = 一次性应用」是断言保证的，不是感觉。** `test_patch.py` 断言两层改动的字节集交集为空集，且 `len(a)+len(b)==len(a|b)`。有这条，「先打第一层再打第二层」和「两个补丁一起打」才在数学上等价。

**只读操作不再自动进DFU。** 之前 `verify`/`backup` 会自己 kick 进 DFU，然后要等满180秒。受控对照实验（用例A：release+close；用例B：主机零动作）证明180秒是 bootloader 的绝对时限，从「进DFU那一刻」起算，主机做什么都改变不了。既然催不动，那就少进去 —— 改成只有 `--kick` 才进。

## 绕了远路的地方

**Python 测试的合成镜像构造顺序错了三轮。** `test_profiles.py` 报 `ValueError: 旧字节不匹配：0x3453c`，我以为是补丁记录写错了，实际是 `setUp` 里造镜像的顺序反了：`origin` 必须同时满足**两层**的 before 字节（两层偏移不重叠，所以能共存），而不是只填第一层的。修好后 3 errors + 1 failure 一起消失。

**`test_catalog_shape` 的哈希唯一数断言成了链式结构的证据。** 断言「6个哈希有6个唯一值」失败，实得5个 —— 因为第一层的 `target_code_sha256` 与第二层的 `source_code_sha256` 天然相同。这不是bug，这正是链的定义。改成断言5个唯一值，并**额外单独断言** `clean.source == ref.target`，把「碰巧相同」变成「必须相同」。

**`--gui-smoke` 的「勾选框恰好2个」断言差点挡住新UI。** 这个断言原本的作用是「GUI里没有第二个写入门」。15个部件如果用 `ttk.Checkbutton`，这个断言就得改，而改掉它就失去意义了。改用 `tk.Listbox(selectmode='multiple')`，控件天然不同，断言不用动，原意也保住了。

**`✔` 字符撞上 Windows GBK 控制台。** `render.py` 成功行带 U+2714，从 Python 子进程读 stdout 时直接 `UnicodeEncodeError: 'gbk' codec can't encode character '\u2714'`。修法是给子进程环境加 `PYTHONIOENCODING=utf-8` 和 `PYTHONUTF8=1`。这类问题只在 Windows 上出现，且只在子进程输出里出现，本地单跑不会暴露。

**打包白名单漏了 `dashboard/`，差点发布一个没有屏幕部件页的程序。** 发布链路原本只收集 `tools/wallpaper/firmware/examples/docs/node_modules`，新模块不在里面。同一个位置还藏着三处：版本号在 `build_exe.py` 里硬编码 `1.1.3`（已改为读 `package.json`）、CI 的 tag 过滤只认 `v1.1.*`（已放宽到 `v1.*`）、以及 `_python()` 在冻结后回退到 `sys.executable`——那是主程序自己，让它当 Python 解释器是错的。

**打包版不能假设目标机器有 Python 和 Pillow。** 主程序、`push.js`、`selftest.js` 三处都会调 `render.py`，如果都靠「目标机器的 python」，那么打包就等于没打。解法是把 `dashboard/render.py` 单独冻结成 `runtime/Neos75-Render.exe`（自带解释器和 Pillow），由 `NEOS75_RENDER` 环境变量传给下游；源码运行时该变量为空，走原来的「python + 脚本」。**两条路径共用同一份 render.py**，不存在两套渲染逻辑。冻结后 `__file__` 落在临时解包目录，所以 `render.py` 的 `_here()` 要按 `_MEIPASS/dashboard` → `_MEIPASS` → 脚本同目录 回退着找 `widgets.json`。

**打包版缺 `desktop/dash_bridge.py`，屏幕部件页会整块消失。** 白名单补 `dashboard/` 之后，`desktop/` 仍不在里面 —— 但 `app.py` 是靠 `importlib` 从 `PROJECT/desktop/dash_bridge.py` 动态加载桥接层的，缺了它 `dash()` 直接抛异常，`board_selftest`/`board_registry` 两项变成 `SKIP`，界面上的「屏幕部件」页也读不到注册表。修法是把 `desktop` 也加进白名单，但**只带 `dash_bridge.py`**（构建脚本 `build_exe.py`/`ci_smoke.py` 不需要进包）。这个错误只有真正跑打包版的自检才会暴露 —— 源码模式一切正常。

**PyInstaller onefile 引导在受限目录里会静默卡死。** 打包版 `--self-test` 在本机工作区里永远不返回、CPU 恒为 0、连 `--badflag` 这种应立即报错的调用都挂住。逐步 bisect 的结论是：把同一个 exe 复制到 `D:\文档\deepseek-harness` 及以上层级、或 `C:\tmp`、`D:\tmp` 都能秒出结果；只要放进 `default-workspace` 及其任何子目录就必挂。长度不是原因（同长度纯字母名、长 ASCII 深路径都正常），内容也不是（含中文的 `D:\中文测试` 正常），而是**那棵目录树的 ACL**：`default-workspace` 上有一条 `Everyone: Deny DeleteSubdirectoriesAndFiles`，onefile 引导建临时目录时被这条拒绝掉，就停在那里不报错也不退出。这是本机工作区的环境限制，不是程序缺陷 —— CI 的 runner 路径是干净的 ASCII。`ci_smoke.py` 现在遇到非 ASCII 路径会先把 exe 复制到系统临时目录再测，并把这件事打印出来，避免下次再把环境问题误判成程序问题。

**CI 里从没装过 Pillow，v1.2.0 打了个 tag 却什么都没发出去。** 打完 tag 我以为发布完了，实际 CI 在每个平台都挂在同一步 `Test source without a USB device`：`dashboard/render.py` 里有 `from PIL import Image`，而 `npm test` 的最后一步 `dashboard/selftest.js` 会调 `render.py --list` 做注册表交叉校验，干净 runner 上没有 Pillow 直接 `ModuleNotFoundError: No module named 'PIL'`。本机恰好装着 Pillow 12.3.0，所以本地怎么跑都是绿的。**「本地全绿」和「CI 全绿」之间差着一句 `pip install`** —— 修法是把它钉进 `requirements-dev.txt`，workflow 改成 `-r requirements-dev.txt`，两边共用同一份清单。教训：新增任何第三方 import，先去 `requirements-dev.txt` 里认领一行。

**`requirements-dev.txt` 里的中文注释会让 pip 装不上。** 我按习惯给 Pillow 那行写了中文注释，pip 读 requirements 文件时用的是平台 locale，在 GBK 控制台上直接 `UnicodeDecodeError: 'gbk' codec can't decode byte 0xa8 in position 53`。**requirements 文件必须纯 ASCII** —— 想写说明就用英文，或者把说明放到文档里。这个错本地复现一次才发现（`python -m pip install -r requirements-dev.txt` 报错的位置和 CI 完全不同，CI 上是先炸 Pillow 导入，本地反而是先炸文件解码）。

**字体路径写死 Windows，macOS 上中文会全变方块。** `render.py` 原本是 `FONT_REG = r"C:\Windows\Fonts\msyh.ttc"` 这种写法，在 macOS runner 上 `ImageFont.truetype()` 直接抛异常退回 Pillow 内置字体 —— 内置字体不含汉字，15 个部件的中文标签会全渲染成豆腐块。CI 矩阵里有 `macos-15` 和 `macos-15-intel` 两条，这雷一定会踩。改成按平台探测候选列表（Windows 微软雅黑/Consolas → macOS 苹方/Menlo → Linux Noto/DejaVu），取不到才退回内置字体。顺带发现 `ImageFont.load_default()` 的 `.path` 是个 `BytesIO` 而不是文件路径，所以「回退」相关的断言必须能跳过，不能硬比 `.path`。

## 留下的债

- `full` 预设下 CPU/内存大数字顶部被裁（7行缩放后卡片高度不足），未修。
- `temp_c` 在本机恒为 `null`（两个热区数据源都读不到），部件显示 `n/a`。
- 推送后原厂协议无最终ACK，`displayVerified` 恒 `false`，只能靠看屏确认。
- 部件只有15个，后续可以继续加（用户明确说了「后面再持续多加几个功能」）。
- Mac 版 USB/DFU 未在真机验证。
- 屏幕主控 UB32F341S 的 SWD/BOOT0 焊盘从未找到：如果屏幕代码区刷坏，目前没有恢复通道。

## 后续风险清单（按后果×概率排序）

| 风险点 | 触发条件 | 后果 | 前兆 | 对策 |
| --- | --- | --- | --- | --- |
| 两层层序颠倒或重复应用 | 手动改配置、把第二层当第一层用 | 旧字节校验失败，拒绝生成（不会写坏） | 报「旧字节不匹配」 | 只认哈希衔接；`test_patch.py` 断言两层字节集不重叠；链末拒绝重复刷写 |
| 第二层被套到别的构建上 | 代码哈希相同但布局不同（理论上不可能，实际靠哈希兜底） | 拒绝生成 | 生成前报哈希不符 | 生成前必须匹配 `source_code_sha256`，不匹配直接报错退出 |
| 连续推送被打断 | 关窗口、断USB、官方程序同时上传 | 屏幕停在半张图 | 画面花屏或不完整 | 推送前提示看屏；`board_loop_stop()` 显式 terminate；同一时刻只允许一个上传者 |
| 部件画挂导致整屏空白 | 某个数据源返回意外类型或空值 | 该卡片显示 `n/a`；极端情况整图渲染失败 | 渲染子进程非零退出 | 单个部件异常只画 `n/a` 卡片不拖垮整图；`--debug` 把异常打到 stderr |
| 采样拖慢推送周期 | CPU 高负载或 WMI 变慢 | 实际刷新变慢，用户以为卡住 | 每轮 JSON 里的「采样ms」变大 | 每轮打印采样/渲染/推送/周期四项耗时；重项按相位分摊缓存 |
| 温度永远读不到被误当成bug | 机器不支持热区WMI | 用户以为程序坏了 | 界面上温度恒定 `n/a` | 文档明示数据源与限制，不填假数据 |
| 为只读操作进DFU白等180秒 | 误加 `--kick` 或旧脚本仍在用 | 浪费时间，屏幕不显示画面 | 命令提示「不在DFU」后卡住 | 只读操作默认不 kick；刷写流程内复用已接管会话免费回验 |
| 打包版缺桥接层导致部件页消失 | 白名单未收 `desktop/dash_bridge.py` | 屏幕部件页整块不可用，自检两项变 SKIP | 自检里 `board_*` 显示 `SKIP`，界面读不到注册表 | 白名单收 `desktop/` 且只带 `dash_bridge.py`；打包版自检必须真正跑一遍 |
| 打包版在受限/非 ASCII 目录下卡死 | 目录 ACL 禁止建子目录，或路径含非 ASCII 字符 | 引导阶段静默挂住，连参数错误都不报 | 进程 CPU 恒 0、无任何输出、无结果文件 | `ci_smoke.py` 检测到非 ASCII 路径就先复制到系统临时目录再测并打印原因；本地验证放到 `C:\tmp` 一类干净路径 |
| 尾段现场数据被当成固件写回 | 刷写工具误把设置区列入待写扇区 | 用户设置回滚成旧值 | 刷后设置变化、该扇区字节数异常 | `screen_mcu.js` 内 `KEEP_SECTORS` 硬保护该扇区，永不列入待写 |
| 打包版找不到部件注册表 | 冻结后 `widgets.json` 未随数据文件打包 | 屏幕部件页空白、下拉无内容 | 界面提示「部件注册表读取失败」 | `render.py` 的 `_here()` 按 `_MEIPASS/dashboard` 回退；`FrozenRenderer` 三个用例覆盖有/无注册表两种情况 |
| 打包版被当成源码版走 python | `NEOS75_RENDER` 未传或指向不存在的文件 | 渲染直接失败，提示找不到 python | 渲染子进程报 `FileNotFoundError` | `rendererCommand()` 与 `_renderer()` 只在文件真实存在时才用 exe，否则退回脚本；`NEOS75_RENDER` 由 `push_env()` 统一注入 |
| CI 因版本号或 tag 不匹配不发版 | `package.json` 版本与 tag 前缀不一致 | 打了 tag 却没有 Release | Actions 里 workflow 未触发 | 版本号统一从 `package.json` 读；tag 过滤放宽为 `v1.*`；构建后 `ci_smoke.py` 仍会验证自检与 GUI |
| CI 缺依赖，本地全绿但发不出版 | 新增第三方 import 未写进 `requirements-dev.txt` | 三个平台全部失败，无 Release；本机完全复现不出 | 本地怎么跑都过、CI 挂在同一步「Test source」 | 新增依赖必须同步 `requirements-dev.txt`；workflow 与本地共用同一份清单；发版前先本地跑一遍干净 venv |
| requirements 文件写入非 ASCII 字符 | 在依赖行旁写中文注释 | pip 用 GBK 解码失败，装不上任何依赖 | `pip install -r` 报 `UnicodeDecodeError: 'gbk' codec` | `requirements*.txt` 保持纯 ASCII；说明写进文档而不是注释 |
| 非 Windows 平台字体缺失导致中文成方块 | 字体路径写死 `C:\Windows\Fonts`，目标机没有该字体 | 界面上所有中文标签渲染成豆腐块，功能正常但不可读 | macOS/Linux 上截图里中文全是空心方框 | 按平台探测候选字体列表；取不到才退回内置字体；测试里对「回退」分支用 skip 而不是失败 |
