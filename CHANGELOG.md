# 更新记录

## v1.7.0 — 2026-11-04

修一个把普通人挡在门外的分支：哈希不认识的固件，桌面程序以前直接报「设备固件不匹配」，
于是他们只能去勾「无验证」——而强制模式盲套固定地址，**上传进度条就留在屏幕上**。
v1.6.0 做的内容锚点适配只存在于命令行，桌面这条路没接上。

- **生成器接上适配器**（`tools/build_fullscreen.py`）：`patch()` 在 `unsupported-version` /
  `already-fullscreen` 时不再早退，先调 `adapt()` → `adapt_firmware.build_adapted()`。
  成功返回 `adapted=True` + 现算出的 `target_sha256`；失败时用 `_adapt_reason()` 把
  各层的 blocker 压成一句人话（签名对不上 / 布局差得远 / 旧字节位置被改），而不是一句
  「不匹配」。
  - 兄弟模块定位改成 `Path(__file__).resolve().parent`：以前用 `ROOT`，测试把
    `build_fullscreen.py` 连同临时清单一起加载时 `ROOT` 指向临时目录，拼出来的
    `adapt_firmware.py` 并不存在（`test_profiles` 两个用例因此报 FileNotFoundError）。
- **刷写端改为核对产物而不是重算**（`tools/mcu_flash_live.js`，新增 `--expect <sha256>`）：
  Node 只有固定偏移，重算必然算不出适配产物。现在校验「文件本身」——SHA256 与生成端
  报的一致、尾部 4096 字节现场数据未动、前 256 字节向量表未动、有差异的扇区落在已知
  补丁扇区白名单内。这四条**上移到所有模式之前**，所以 `plan` 只读预检同样受保护。
- **别在擦除前空口承诺**：去进度条那一层在链中间，适配器可能只套上了后面的注入层。
  所以新增 `progress_removed()` 判定并把 `progressRemoved` 一路传到界面：真套上了才说
  「进度条同样会被去掉」，漏了就说「但还是没能套上去进度条那一层，浏览器上传时进度条会保留」，
  完成标题也跟着变成「补丁已完成（进度条仍在）」。`_friendly()` 不再把适配器给出的人话
  盖成「操作已停止」。
- **打包补上两个隐藏依赖**（`desktop/build_exe.py`）：`tools/build_widget.py` 是运行时
  importlib 加载的，里面用 keystone 重新汇编分支、用 capstone 校验，PyInstaller 静态分析
  看不见 —— 打包版一碰到陌生固件就报 `No module named 'keystone'`。现在用
  `collect_dynamic_libs()` 把 `keystone.dll`、`capstone.dll` 一起带上，体积从 103.5 MB
  涨到 106.1 MB。
- **链里「改动量」不能相加**：`widget` 把页掩码写成 `0x3f`、`widget-none` 再改成 `0`，
  两层改的是同一批字节，求和必然虚高，旧校验 `changed == 每层之和` 在 clean 镜像上直接失败。
  改成「不得超过各层之和」，产物对不对仍由那条 `target_code_sha256` 说了算。
- **已经是最新版就别去试适配**：链末镜像的空闲带已被占用，跑适配器会抛
  `命令表 0x3D 项与预期不符`，用户看到的是内部错误。改成在 `already-fullscreen` 时先说
  「备份已是全屏版」。
- **keystone 变成可选件**：CI 四平台全挂在同一个 `No module named 'keystone'` ——
  仓库依赖清单里根本没写它，本机却早就装好了，所以本地永远绿。现在写进
  `requirements-dev.txt`，但 PyPI **不给 macOS arm64 出 wheel**，那一档用环境标记跳过。
  缺了它时注入层降级（`build_widget.keystone_ready()`），而 `best_chain()` 改成取
  「最长可用前缀」—— 断在注入层不再连累前面的层，去进度条照样生效；掉队的层写进
  `dropped` 报给上层。打包脚本同样改成 capstone 必需、keystone 有就带上。
- **陌生固件也要过一道物理检查**：适配把「不认识」变成「照样打补丁」，代价是坏备份
  也会被当成新版本。`plausible_vectors()` 只查两件必然成立的事 —— 栈顶落在 64 KB RAM 里、
  复位向量（镜像内偏移 + `0x08008000`）落在代码区。随机字节和半截镜像因此被挡在适配器之前。
  实测这四个向量值是 `SP=0x2000c548`、`reset=0x81bd`，四份已知镜像完全一致。
- 测试：`tests/test_adapt.py::BuilderWiring` 7 项（未知固件必须适配、进度条去没去要说得准、
  已是最新版要给人话而不是内部错误、无 keystone 时前缀降级、向量表崩坏先拒、尺寸不对仍拒绝、
  垃圾镜像给可读原因）；`test_glass.py`
  的旧用例改名 `test_unknown_firmware_is_still_adapted_instead_of_refused`（反过来钉死
  「不许再拒绝」），并新增 `test_unadaptable_firmware_keeps_the_backup_and_never_writes`
  （套不上时不刷、备份留、报错含原因）。全量 **130 项 Python + 8 组 node 全绿**。
- 实测：clean 镜像插 0x400 字节后自动适配 `changed=2369`、`adapted=true`、尾部未动；
  原版改版本字节后三层全套 `changed=2409`；四条负向用例（改尾部/改向量表/改未预期扇区/
  文件被换）各自拒绝且文案点名。

## v1.6.0 — 2026-11-04

同款键盘的固件版本五花八门，以前每来一份都要人工反汇编找新偏移、单独做一份 profile。
这一版把「按偏移打补丁」换成「按内容锚点定位」，陌生人把备份拖进 issue 就能拿到可刷镜像。

- **内容锚点**（`tools/make_anchor.py` → `firmware/anchors/*.json`）：每条补丁记一段
  **全片唯一**且**不盖住本层任何被改字节**的上下文。`reference` 20/20、`clean` 5/5、
  `widget-none` 2/2 都有锚点；注入层的两条本来就锚不住（那段代码含绝对地址）。
  - 踩过的最贵的坑：第一版是「在新固件上从旧偏移取上下文当指纹」，布局一迁移就取到
    **错位**的字节，而那段错位字节恰好也唯一 → 算出「位移 0」，看起来成功、实际写到
    完全不相关的位置。**锚点必须从这一层的源镜像预先算好落盘**，适配时只拿它去搜。
  - 第二个坑：旧版锚点只避开「自己这条补丁」，会盖住同层其它补丁的字节，上层打完就
    搜不到了，于是把「已打过」误判成「没打过」、整链 blocked。现在按 `forbidden`
    区间躲开、并要求源镜像与目标镜像**两边都唯一**。
- **符号签名**（`tools/symbols.py` → `firmware/anchors/symbols.json`）：11 个厂商函数
  各取 16 字节入口当签名，加 `TBB`、`CMD_RET` 两个结构锚点，共 13 项。在 clean 与
  原版两份镜像里都能唯一定位、偏移一致。
  - 结论：**RAM 变量不需要签名**。它们是固定 `.bss` 地址，跨固件不变；需要重定位的
    只有函数代码。（实测在 clean 镜像里搜这些地址的 `movw` 引用是 0 处 —— 那些引用
    只存在于注入代码里。）
- **可重定位构建器**（`tools/build_widget.py`）：`TICK`/`ENSURE` 从模块级 f-string 改成
  函数（否则 import 时就把符号值冻死了）；新增 `relocate(syms, free)` / `restore(old)` /
  `visible_symbols()`；`patch(source, pages, strict=True)` 加 `strict=False`（只在符号
  已换好时用）。命令项写入改成 `(F_RECEIVE - (CMD_TBB + 4)) // 2` —— **必须 +4**，
  `tbb [pc,r]` 用 pc+4 作表基址。往返验证：迁移 +0x400 再 `restore()`，与基线逐字节一致。
- **链式适配器**（`tools/adapt_firmware.py`）：`build_adapted(source)` 一条命令出完整
  镜像（含 DFU 后缀与 CRC 反码）。每条补丁三种结局：原位命中 / 按锚点搬移 / 拒绝
  （`unlocatable`、`mismatch`、`injection`）。`--build` 退出码有意义：`0` 成功、`3` 点
  对不上、`4` 已经是最新。
  - 实测五种输入：原版迁移 → 套三层 2409 字节；clean 原位 → 只套 widget 2369 字节
    （产物 sha 与仓库里 `ov6q.bin` 的 `target_sha256` **完全一致**）；全屏原位 → 套两层；
    设备当前 `ov6p` → 走「只换页掩码」路径，366 字节，代码指纹落到 `ec412e092dc8`。
  - 两条独立路线（直接 `build_widget.patch` vs 适配器的 `apply_entries`）在三份迁移镜像
    × 两种页掩码下产物**逐字节相同**。
- **issue 入口**（`tools/adapt_issue.py` + `.github/workflows/adapt.yml` +
  `.github/ISSUE_TEMPLATE/firmware-adapt.md`）：把 `.bin` 拖进 issue、打 `firmware-adapt`
  标签，机器人算完回帖给出镜像与派生 profile。全程不接设备、不刷写，只算字节。
  - 护栏：长度必须是 491520/491536、代码区不能一半以上是 `0xFF`；抓不到附件只回
    「重新拖一次」，不抛栈给普通用户看。
- 测试：新增 `tests/test_adapt.py`（18 项：锚点文件完整性、签名覆盖、定位唯一性、
  三种结局、issue 输入护栏、真机镜像端到端）。全量 **121 项 Python + 8 组 node 全绿**。

## v1.5.1 — 2026-11-04

两个实测反馈的修复。两个问题都不是「参数没调好」，而是**逻辑走错了分支**。

- **强制模式改成「先试后写」**：旧的 `--force` 在固件布局对不上时直接套链首最基础的那一层，跳过了「去掉上传进度条」的补丁 —— 于是用户刷完发现浏览器上传时进度环照旧。现在强制模式会**从链首开始逐层核对旧字节**，对得上才应用，任一条对不上就停在该层之前，并在多条链里取能套最多的那条。新增 `force_layers()` / `force_head()`（Python 与 Node 逐行等价；`roots`/`forceLayers`/`forceHead`/`planFor` 已导出）。
  - 实测三档：known-clean 固件强制 → 套两层（`widget` + `widget-none`），2369 字节；v1.5.0 的设备固件强制 → 只套 `widget-none` 366 字节；把 clean 镜像改一个字节（模拟布局迁移）→ 一层都对不上，`forceBlind=true`、改动 0 字节。
  - **不再允许「半套」**：必须从链首连续判断，不能从中间层起步，否则会出现「clean 层没套上、却把挂在它后面的 widget 层单独套进去」的状态，比不套更危险。
  - 强制模式下若一层都对不上，程序会在擦除前把话说清楚：**「进度条不会被去掉」**，完成提示也改成「补丁已完成（进度条仍在）」，不再假装成功。
  - 尾部 4096 字节现场数据保护从「仅非强制」改为**无条件校验**（强制模式原本能改用户配置区）。
- **文字组件默认关闭 + 一键关掉**：v1.5.0 把页掩码写死成全六页（`0x3f`），结果每一页都叠四行字，还压住原厂的状态栏。
  - 固件侧：`build_widget.py` 的 `TICK` 段新增 `tk_clear` 分支 —— 页掩码里没选中本页时，把本页四个 label 指到空串 `D_NONE=52`、`C_APPL` 置 `-1`（靠 `-1+1==0` 保证只清一次），于是**没选中的页面恢复原厂画面**；CLI `--pages` 默认从 `6` 改为 `none`。
  - 新 profile `firmware/profiles/widget-none.patch.json`（`widget-none-ec412e09`，挂在 `widget-dce6dce7` 之后，只改 366 字节 / 2 扇区）：把「全页开」改成「全页关」。老用户从 v1.5.0 升上来只需这一层，不用重刷 2369 字节。
  - 协议层：`maskPacket` 与 `maskFromPages` 放开掩码 0（空选 → 0）；真机实测掩码 0 回 `0xFE`，随后 `probe` 变 `0xFD`，是**干净的关闭**。
  - 桌面程序：新增「关闭文字组件」按钮，点一下把页选择清空并写掩码 0；`native_pages()` 不再对「一个都不选」报错，改为走关闭路径并提示「所有界面恢复原厂内容」。
- 测试：`tests/test_glass.py` 新增 `test_native_off_switch_writes_mask_zero`，原 `test_native_mask_request_refused_before_usb_access` 改名为 `test_native_start_needs_at_least_one_page`（连续发送仍需至少一页，关掉是另一条路径）；`tools/test_native_widget.js` 两个断言组同步改写。全量 **103 项 Python + 8 组 node 全绿**，`--gui-smoke` 增加「关闭入口存在 / 空选映射到掩码 0 / 选页保序」三条断言。

## v1.5.0 — 2026-11-03

- **任意页覆盖组件**：新增 `firmware/profiles/widget.patch.json`（挂在全屏+清爽之上，代码区SHA256精确衔接）。固件改动是覆盖式的——只改命令表 `0x35316` 两个字节 + 往代码区空闲带 `0x4aacc` 放代码，**页表与六个原厂页面处理函数一个字节都没动**（构建器用断言守住这两条）。改动 2369 字节，触及 3 个 4KB 扇区。
- **六个页面可以各自放不同的字**：每页独立缓冲；未选中的页面完全保持官方原样，选中的页面在原图之上叠四行文字。选页是运行时写内存决定的，随时可改，不用重刷固件。
- **原生文字通道**：新增 `tools/native_widget.js`（协议编解码）、`tools/push_widget.js`（命令行 + 常驻 loop）、`dashboard/native_rows.py`（采样状态→四行 ASCII）。私有命令 `0x3D`，每屏四行、每行 47 字符、只支持 ASCII；不擦 Flash、不进 DFU、不占用 JPEG/GIF 上传队列，所以刷新能压到两三秒一次（实测 3 秒一帧稳定）。
- 桌面程序「小组件」页底部新增「直接写进键盘内存的四行文字」面板：六个页开关、四行预览、检查设备（只读）、应用界面选择、发送一帧、连续发送；刷新间隔 2/3/5/10 秒。
- `npm test` 增加 `tools/test_native_widget.js`（12 组离线用例）；新增 `tests/test_native_rows.py`（10 项）与 `tests/test_glass.py` 的六个原生通道用例。全量 102 项 Python 测试 + 8 组 node 套件全绿。
- 修复：`--gui-smoke` 增加了「六个页按钮、四行预览」断言，界面回归能被自动抓住。
- 已知限制：中文会被替换成空格；取不到的传感器写 `n/a`；无线模式未验证；长跑仅 40 轮。

## v1.2.0 — 2026-11-03（已发布）

发布产物：Windows x64 单文件 EXE、macOS arm64 / x64 原生应用。三平台 CI 全绿。

- **补丁链**：固件配置支持多层叠加。新增 `firmware/profiles/clean.patch.json`（全屏+清爽），它挂在原验证构建之上，代码区SHA256精确衔接（`clean.source == reference.target`）。一个备份可自动连打多层，逐层与一次性结果完全一致。
- **全屏+清爽增强（第二层，真机已刷入并逐字节回验）**：去掉上传时的绿色进度环、去掉中央常驻的`0%`、上传期间不再隐藏原图（消除满屏黑），并把上传收尾阈值3000ms降到1500ms。实测刷新由约33秒一次变为3~4秒一次，用户确认。
- **屏幕部件（可选显示）**：新增 `dashboard/` 模块。可在屏幕上显示时钟、日期、CPU、内存、GPU、CPU历史、最占进程、磁盘、网络速率、IP、主机名、电池、音量、运行时长等15个部件，支持5种预设与任意勾选组合。
- 桌面程序新增「屏幕部件」页：勾选部件 → 采样电脑状态 → 480×360预览 → 推送一帧 / 连续推送。采样与渲染完全离线，只有推送才连接键盘。
- 打包版自带独立的渲染器（`runtime/Neos75-Render`），不依赖目标机器安装 Python 与 Pillow。
- `npm test` 增加 `dashboard/selftest.js`；离线自检新增部件注册表与推送协议两组交叉校验。
- 修复：CI 缺少 Pillow 导致三个平台全部失败、`release` 被跳过（`requirements-dev.txt` 补齐，workflow 改为共用同一份依赖清单）。
- 修复：渲染器字体路径写死 Windows，macOS 上中文会渲染成方块；改为按平台探测候选字体。

## v1.1.3 — 2026-11-03

- 按备份代码哈希自动选择原验证构建或Issue #1构建的对应补丁，生成、刷写和恢复贯通，无需勾选无验证模式。
- 同时识别两种全屏代码，显示配置和验证状态；新构建标明离线通过、实机待验收。
- 增加双配置自动选择及真实备份离线核验摘要，设备完整转储继续保留本地。

## v1.1.2 — 2026-11-03

- 增加默认关闭的“无验证，直接打补丁”选项及 `--force`；绕过版本哈希与旧字节检查，生成、刷写及恢复流程贯通。
- 保留原始备份、目标一致性和写后回验，新增未知固件强制流程模拟及打包后验证。

## v1.1.1 — 2026-11-02

- Windows单文件EXE、macOS Apple芯片/Intel原生应用及三平台构建流程。
- 无需已有备份的设备双读备份入口、版本识别、失败保留.partial。
- 图形界面查询、补丁生成、JPEG测试和固件维护，内置运行环境。
- 启动/门控/协议/备份检查，以及Mac硬件尚未验收的说明。

## v1.0.0 — 2026-11-02

- 480×360图片/GIF全屏补丁，用户实屏验收通过。
- 每4096B块独立定位的MCU DFU工具，四个8KB组含邻居数据回验。
- 精确版本门控与可复现生成器、JPEG上传工具、离线测试。
- 记录首次块定位错误、恢复经过和后续风险。
