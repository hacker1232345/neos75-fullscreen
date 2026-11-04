"""把四套配色拼成一张对照图，方便一眼比较。

用法：
    python make_theme_sheet.py                     # 用自带示例数据
    python make_theme_sheet.py C:/tmp/real.json    # 用真实采样
    python make_theme_sheet.py in.json out.png     # 指定输出位置

不带参数时不依赖任何外部文件，所以在 CI 或别人机器上也能跑。
"""
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('r', ROOT / 'dashboard' / 'render.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

DEMO = {
    'clock': '12:34:56',
    'date': '2026-11-05',
    'cpu_load': 34.5,
    'cpu_freq_ghz': 2.4,
    'mem_pct': 62.0,
    'mem_used_gb': 19.8,
    'mem_total_gb': 31.5,
    'gpu_pct': 12,
    'temp_c': 56.0,
    'disk_free_gb': 210.4,
    'net_down_mbps': 1.24,
    'net_up_mbps': 0.08,
    'bat_pct': 78,
    'bat_ac': True,
    'bat_charging': False,
    'vol_pct': 45,
    'uptime': '1d 06:51',
    'top_proc': 'chrome',
    'top_proc_pct': 8.3,
    'ip': '192.168.1.26',
    'host': 'DESKTOP',
    'weather_text': '多云',
    'weather_temp': 18,
    'weather_humidity': 62,
}

WIDGETS = ['clock_big', 'date', 'cpu', 'mem', 'temp', 'disk', 'net', 'battery', 'uptime']


def sheet(state, out):
    from PIL import Image, ImageDraw, ImageFont

    cells = []
    for name in m.THEME_NAMES:
        data, img = m.render(state, widgets=WIDGETS, quality=90, theme=name)
        cells.append((name, img))

    pad = 12
    label_h = 26
    w, h = cells[0][1].size
    W = w * 2 + pad * 3
    H = (h + label_h) * 2 + pad * 3
    canvas = Image.new('RGB', (W, H), (24, 24, 26))
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 15)
    except OSError:
        try:
            font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 15)
        except OSError:
            font = ImageFont.load_default()

    for i, (name, img) in enumerate(cells):
        col, row = i % 2, i // 2
        x = pad + col * (w + pad)
        y = pad + row * (h + label_h + pad)
        theme = m.THEMES[name]
        draw.text((x + 2, y + 3), '%s  %s' % (name, theme['label']), font=font, fill=(230, 230, 232))
        canvas.paste(img, (x, y + label_h))

    canvas.save(out)
    return out


def main(argv):
    state = DEMO
    out = 'theme_sheet.png'
    if len(argv) >= 2:
        state = json.loads(pathlib.Path(argv[1]).read_text(encoding='utf-8'))
    if len(argv) >= 3:
        out = argv[2]
    for key, value in DEMO.items():
        state.setdefault(key, value)
    path = sheet(state, out)
    sys.stdout.write('sheet -> %s\n' % path)


if __name__ == '__main__':
    main(sys.argv)
