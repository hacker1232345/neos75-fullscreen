# 本机维护与恢复

本次已刷设备无需重复执行。工具绑定已验证的代码区域SHA256和全部补丁原字节，保留每台设备自己的末尾4KB数据；目标须与当前备份重新生成的结果逐字节一致。不得把屏幕MCU工具用于键盘主控、接收器或屏幕UI资源片。

维护前保留完整原始备份，匹配备份放仓库根目录 `mcu-before-fullscreen.bin`；先离线生成目标文件。Windows设备需要可用的USB/DFU驱动，依赖 `usb` 与 `node-hid` 由npm安装。

以下命令面向已确认版本和恢复路径的维护者：

```sh
node tools/mcu_flash_live.js plan tools/mcu-fullscreen-live-v1.bin
node tools/mcu_flash_live.js flash tools/mcu-fullscreen-live-v1.bin mcu-before-fullscreen.bin --yes
node tools/mcu_flash_live.js verify tools/mcu-fullscreen-live-v1.bin
```

`plan`不擦写，但可能进入DFU。`flash`先读取四组进行预检，只接受原始或目标内容；保存回滚快照、擦除后检查全FF、每块独立定位写入、逐字节回验。不自动重发program。最终不自动退出DFU，维护者确认回验通过后可使用 `node tools/leave_mcu.js`。

恢复原始四个保护组使用：

```sh
node tools/mcu_flash_live.js restore tools/mcu-fullscreen-live-v1.bin mcu-before-fullscreen.bin --yes
```

restore同样需要目标文件来确定修改范围，恢复的是原始四个保护组，不含其他组件。现场不完整时restore允许未知组内容；必须先确认原始备份、地址和DFU状态。出现回验不符立即停止；没有可用DFU时该脚本不能替代硬件维修。

日常JPEG发送（会替换壁纸）：

```sh
node wallpaper/screen_wallpaper.js status
node wallpaper/screen_wallpaper.js plan examples/fullscreen-test-1.jpg
node wallpaper/screen_wallpaper.js send examples/fullscreen-test-1.jpg --yes
```

原厂上传尾段无最终ACK。工具返回displayVerified:false，必须看实屏确认，且不要同时开其他上传程序。GIF日常使用官方程序导入。


v1.1.3的原始备份按哈希自动选择对应构建配置。两种已知构建均无需 `--force`，生成和维护时使用同一份本机原始备份与其生成目标。Issue #1构建经过离线定位和模型验证，实屏待验收。
