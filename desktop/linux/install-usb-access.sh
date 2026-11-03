#!/bin/sh
set -eu
if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' '首次使用需要安装 Neos75 USB 权限规则。请执行：sudo ./install-usb-access.sh' >&2
    exit 1
fi
bundle_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install -m 0644 -- "$bundle_dir/70-neos75.rules" /etc/udev/rules.d/70-neos75.rules
udevadm control --reload-rules
printf '%s\n' '规则已安装。请重新插拔键盘，然后以普通用户运行 ./Neos75。'
