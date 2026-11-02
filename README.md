# AULA Neos75 图片 / GIF 全屏补丁

把官方自定义媒体页从右侧320×320区域扩展为 **480×360全屏**，隐藏左侧状态条。2026-11-02已在一台Neos75上刷入并逐字节回验，用户确认：**图片和GIF都全屏正常**。

![全屏验收测试动画](examples/fullscreen-test.gif)

上图是生成的验收素材，非屏幕实拍。实机确认见 [验收记录](evidence/user-acceptance.json)。

## 日常使用

已经刷入本补丁的设备，重新连接官方程序后，在第5页导入原图片或GIF即可。旧320×320转换素材需重新导入。非480×360原图的缩放/裁切由官方程序控制。

## 内容

- `firmware/fullscreen.patch.json`：精确旧/新字节、地址、原始和目标SHA256；仅改变27个载荷字节。
- `tools/build_fullscreen.py`：纯Python标准库生成器；匹配的用户自有备份可生成本次实测完全一致的固件。
- `tools/mcu_dfu.js`、`mcu_flash_live.js`：MCU DFU传输及带备份/回验的本机专用刷写工具。
- `wallpaper/`：官方壁纸协议的JPEG上传工具和差分测试。GIF全屏由补丁支持，日常GIF导入使用官方程序；本JPEG工具不提供通用GIF转换。
- `examples/`：480×360边框图和两帧GIF。
- `evidence/`、`docs/`：实机回验、仿真结果、验收及纠错复盘。

完整设备备份、原厂固件和生成后的整片镜像由设备持有人本地保存。本仓库发布补丁数据及工具，不包含个人设备完整Flash转储。

## 适配范围

目前只支持实测备份SHA256：

```
c69c38b02841c533db44e8b058a6b86d4a0e816d2bb6ce95cd2b1d5d6927016d
```

备份长度491520字节，映射0x8000..0x80000。不同固件版本和其他设备必须重新定位，生成器会拒绝不匹配输入。设备型号相同不代表固件构建相同。

## 安装与离线验证

需要Node.js及Python 3。无需接设备即可运行默认测试：

```sh
npm install
npm test
python -m unittest discover -s tests -v
```

用已取得、匹配本版本的原始备份生成目标（这一步不连接设备）：

```sh
python tools/build_fullscreen.py --source /path/to/mcu-before-fullscreen.bin --out /path/to/mcu-fullscreen-live-v1.bin
```

输出491536字节（含16字节DFU后缀），目标SHA256：

```
3c922e809dcfac1c35ad8f1f8caebcfc999bf6475a3e6fa22fd18c5d85a4f6e5
```

需运行固件模型测试时，把匹配备份放仓库根目录并以默认输出路径生成，再执行 `npm run test:device-image`；这些文件已加入gitignore。ARM指令仿真额外需要 `pip install -r requirements-dev.txt`，然后执行 `python tools/test_arm_emulation.py`。仿真不覆盖LCD、硬件JPEG和真实堆。

## 刷写和恢复

刷写会修改设备固件。请先阅读 [维护说明](docs/maintenance.md)。本工具为已验证设备保留，不能作为其他Neos75的一键通用升级器。`plan`和`verify`也可能使设备进入DFU，日常只查询屏幕可用 `node wallpaper/screen_wallpaper.js status`。

已验证的写法是每个4096B块独立SET_ADDRESS并以DNLOAD block=2写入。4个8KB保护组为0x10000、0x32000、0x36000、0x42000，包含邻居数据，每组写后回验，最终再次回验。不能套用旧UI连续块算法。

## 验证与限制

实机四组回验PASS，退出DFU后应用回报480×360；静态JPEG和2帧动画传输完成，用户确认两种均全屏正常。长GIF帧率、复杂素材及断电保留尚未验收。LVGL堆384KB，全屏共享缓冲约337.5KB，剩余空间需关注。

复盘与五列风险表见 [项目复盘](docs/retrospective.md)。本项目仅覆盖屏幕全屏媒体，不包含无线实时温度/状态页。

## 来源与许可

原创补丁工具和文档采用MIT许可；品牌、原厂固件及官方前端方法的权利属于原权利人。官方方法摘录仅用于协议差分验证，详见 [第三方说明](THIRD_PARTY_NOTICES.md)。本项目与AULA官方无隶属关系。
