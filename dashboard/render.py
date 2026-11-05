# -*- coding: utf-8 -*-
"""dashboard/render.py —— 把电脑状态 JSON 渲染成 Neos75 小屏用的 480x360 JPEG。

这是一个「可选部件系统」：部件清单来自同目录 widgets.json，
每个部件声明自己的 minWidth/minHeight 与 draw 函数名，
本文件负责按 480x360 自适应网格排布并绘制。

硬约束（来自屏幕协议实测）：
  - 输出必须是 480x360（屏幕自报尺寸，设备侧不自动缩放，尺寸不符直接报错）
  - 必须是 8 位 baseline JPEG（设备不支持 progressive）
  - 文件越小推送越快（每 4096 B 约 66 个 HID report）

用法：
  python dashboard/render.py --in state.json --out out.jpg
  python dashboard/render.py --in - --out out.jpg --preset full
  python dashboard/render.py --in state.json --out out.jpg --widgets cpu,mem,clock_big
  python dashboard/render.py --in state.json --out out.jpg --quality 72 --limit 30000 --preview out.png
  python dashboard/render.py --in state.json --out out.jpg --state-dir .cache --widgets history_cpu
  python dashboard/render.py --list
"""
import argparse
import datetime as dt
import io
import json
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:  # 保证中文 JSON 输出不炸
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent


def _here():
    """widgets.json 所在的目录。

    源码运行时就是本文件所在目录；被 PyInstaller 冻结成独立渲染器后，
    __file__ 指向临时解包目录，数据文件在 sys._MEIPASS/dashboard 下。
    """
    if getattr(sys, 'frozen', False):
        base = Path(getattr(sys, '_MEIPASS', HERE))
        for cand in (base / "dashboard", base):
            if (cand / "widgets.json").exists():
                return cand
    return HERE


W, H = 480, 360
MARGIN = 6
GAP = 4

# ── 主题（对应官方四种配色）────────────────────────────────────
# Venus  银 · Selene 灰 · Hades 黑 · Athena 白
# 键名固定，绘制代码只认这些全局名，apply_theme() 负责整体覆盖。
THEMES = {
    # 黑 —— 现有深色主题，冷蓝强调
    "hades": {
        "label": "Hades 黑",
        "blurb": "深黑底 + 冷蓝强调，原版深色",
        "light": False,
        "BG": (12, 13, 18), "CARD": (26, 29, 38), "CARD_HI": (34, 38, 50),
        "TRACK": (44, 48, 60), "LINE": (40, 44, 56),
        "TXT": (236, 239, 245), "DIM": (130, 140, 158), "FAINT": (78, 86, 102),
        "ACCENT": (94, 200, 255), "GOOD": (86, 214, 139),
        "WARN": (245, 191, 79), "BAD": (240, 92, 92), "NA": (96, 104, 120),
        "TOPLINE": (24, 28, 38), "WATERMARK": (46, 52, 66),
    },
    # 银 —— 拉丝银底 + 香槟铜强调
    "venus": {
        "label": "Venus 银",
        "blurb": "银白底 + 香槟铜强调",
        "light": True,
        "BG": (228, 229, 232), "CARD": (243, 244, 246), "CARD_HI": (252, 252, 253),
        "TRACK": (203, 206, 211), "LINE": (211, 214, 219),
        "TXT": (30, 32, 36), "DIM": (94, 99, 108), "FAINT": (152, 157, 166),
        "ACCENT": (172, 120, 58), "GOOD": (34, 138, 84),
        "WARN": (192, 132, 24), "BAD": (192, 58, 56), "NA": (138, 143, 150),
        "TOPLINE": (208, 211, 216), "WATERMARK": (194, 198, 204),
    },
    # 灰 —— 石墨灰底 + 月光银蓝强调
    "selene": {
        "label": "Selene 灰",
        "blurb": "石墨灰底 + 月光银蓝强调",
        "light": False,
        "BG": (28, 30, 33), "CARD": (40, 43, 47), "CARD_HI": (50, 54, 59),
        "TRACK": (58, 62, 68), "LINE": (52, 56, 62),
        "TXT": (232, 235, 238), "DIM": (146, 152, 160), "FAINT": (96, 102, 110),
        "ACCENT": (168, 196, 224), "GOOD": (120, 200, 150),
        "WARN": (230, 186, 110), "BAD": (226, 110, 106), "NA": (108, 114, 122),
        "TOPLINE": (38, 41, 45), "WATERMARK": (72, 78, 86),
    },
    # 白 —— 暖象牙底 + 墨色文字 + 柔金强调
    "athena": {
        "label": "Athena 白",
        "blurb": "象牙白底 + 墨色文字 + 柔金强调",
        "light": True,
        "BG": (245, 243, 239), "CARD": (254, 253, 250), "CARD_HI": (255, 255, 254),
        "TRACK": (224, 220, 212), "LINE": (229, 225, 217),
        "TXT": (36, 32, 28), "DIM": (110, 104, 94), "FAINT": (170, 164, 154),
        "ACCENT": (176, 136, 56), "GOOD": (52, 138, 82),
        "WARN": (194, 138, 28), "BAD": (188, 64, 58), "NA": (148, 142, 132),
        "TOPLINE": (234, 230, 222), "WATERMARK": (204, 198, 188),
    },
}

def _skin(base, label, blurb, **colors):
    return {**THEMES[base], "label": label, "blurb": blurb, **colors}


THEMES.update({
    "abyss": _skin("hades", "深海青", "深海绿底，薄荷刻度", BG=(16, 38, 43),
                   CARD=(24, 54, 61), CARD_HI=(29, 64, 71), LINE=(44, 79, 83),
                   TRACK=(42, 72, 77), TXT=(233, 248, 245), DIM=(174, 205, 203),
                   ACCENT=(139, 229, 208), NA=(167, 194, 195)),
    "porcelain": _skin("athena", "暖瓷", "暖白釉面，陶土刻度", BG=(245, 237, 224),
                       CARD=(255, 250, 241), CARD_HI=(255, 253, 248),
                       TRACK=(230, 216, 199), LINE=(222, 206, 190), TXT=(51, 38, 32),
                       DIM=(107, 79, 64), ACCENT=(140, 72, 46), NA=(113, 89, 75)),
    "amethyst": _skin("hades", "紫曜", "黑紫色底，淡紫刻度", BG=(28, 22, 39),
                      CARD=(41, 32, 56), CARD_HI=(54, 41, 70), TRACK=(66, 50, 82),
                      LINE=(73, 57, 89), TXT=(248, 237, 255), DIM=(201, 178, 220),
                      ACCENT=(217, 180, 255), NA=(191, 172, 204)),
    "glacier": _skin("venus", "冰湖", "冰白色底，靛青刻度", BG=(225, 238, 243),
                     CARD=(246, 252, 253), CARD_HI=(252, 254, 255), TRACK=(203, 224, 232),
                     LINE=(192, 213, 223), TXT=(23, 48, 65), DIM=(64, 91, 109),
                     ACCENT=(18, 99, 134), NA=(73, 99, 116)),
})
# Use darker signal inks on reflective/light backgrounds; retain meaning in text.
for _name, _accent in (("venus", (123, 76, 33)), ("athena", (119, 82, 22))):
    THEMES[_name]["ACCENT"] = _accent
for _theme in THEMES.values():
    if _theme["light"]:
        _theme.update(GOOD=(23, 104, 71), WARN=(128, 78, 15), BAD=(161, 42, 49))
        if _theme["NA"] == (138, 143, 150) or _theme["NA"] == (148, 142, 132):
            _theme["NA"] = (91, 93, 99)
    elif _theme["NA"] in ((96, 104, 120), (108, 114, 122)):
        _theme["NA"] = (164, 174, 190)

THEME_NAMES = ("hades", "selene", "venus", "athena", "abyss", "porcelain", "amethyst", "glacier")
THEME = "hades"

# 下面这些全局名会被 apply_theme() 按主题覆盖，绘制函数全部引用它们
BG = CARD = CARD_HI = TRACK = LINE = None
TXT = DIM = FAINT = ACCENT = GOOD = WARN = BAD = NA = None
TOPLINE = WATERMARK = None
LIGHT = False


def apply_theme(name, _globals=None):
    """把主题色装进模块全局。名字不认识就退回 hades。"""
    global THEME, BG, CARD, CARD_HI, TRACK, LINE, TXT, DIM, FAINT
    global ACCENT, GOOD, WARN, BAD, NA, TOPLINE, WATERMARK, LIGHT
    key = str(name or "hades").strip().lower()
    if key not in THEMES:
        key = "hades"
    t = THEMES[key]
    THEME = key
    for field in ("BG", "CARD", "CARD_HI", "TRACK", "LINE", "TXT", "DIM",
                  "FAINT", "ACCENT", "GOOD", "WARN", "BAD", "NA",
                  "TOPLINE", "WATERMARK"):
        globals()[field] = t[field]
    LIGHT = bool(t["light"])
    return key


apply_theme("hades")

# 字体：按平台探测。取不到就退回 Pillow 内置字体（中文会变方块，但不崩）。
# Windows 用微软雅黑/Consolas；macOS 用苹方/Helvetica；Linux 用 Noto/DejaVu。
_FONT_CANDIDATES = {
    "reg": [
        r"C:\Windows\Fonts\msyh.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ],
    "bd": [
        r"C:\Windows\Fonts\msyhbd.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ],
    "mono": [
        r"C:\Windows\Fonts\consola.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/Monaco.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ],
}


def _first_font(kind):
    root = Path(getattr(sys, '_MEIPASS', HERE.parent))
    bundled = root / 'fonts/NotoSansCJK-Regular.ttc'
    if kind != 'mono' and bundled.exists():
        return str(bundled)
    for p in _FONT_CANDIDATES[kind]:
        if Path(p).exists():
            return p
    return None


FONT_REG = _first_font("reg") or _FONT_CANDIDATES["reg"][0]
FONT_BD = _first_font("bd") or FONT_REG
FONT_MONO = _first_font("mono") or FONT_REG
# 支持汉字的字体（用于 test 与 F() 的含汉字回退判断）
FONT_CJK = FONT_REG

HISTORY_KEEP = 60

PRESETS = {
    "minimal": ["clock_big", "cpu", "mem", "battery"],
    "default": ["clock_big", "date", "cpu", "mem", "temp", "disk", "net", "battery", "uptime"],
    "full": [
        "clock_big", "date",
        "cpu", "mem", "gpu", "temp", "history_cpu", "top_proc",
        "net", "ip", "host",
        "disk",
        "battery", "volume",
        "uptime", "weather", "codex",
    ],
    "performance": ["cpu", "mem", "gpu", "temp", "history_cpu", "top_proc", "uptime", "clock_big"],
    "network": ["clock_big", "net", "ip", "host", "uptime"],
    "weather": ["weather", "clock_big", "date"],
    "codex": ["codex", "clock_big", "cpu", "mem"],
    # 桌面常驻用：时间 + 正在播放 + 日历 + 两条资源条，一眼能看全
    "desk": ["clock_big", "playing", "calendar", "cpu", "mem", "link", "net_pulse", "battery"],
    # 听歌时用：歌名占一半，剩下留给机器状态
    "media": ["playing", "clock_big", "calendar", "link", "net_pulse", "cpu"],
    "studio": ["focus_timer", "clock_utc", "day_progress", "mem_available"],
}


class UnknownWidget(ValueError):
    """请求了 widgets.json 里不存在的部件 id。"""


def load_registry():
    """读取同目录 widgets.json（顶层是部件对象数组）。"""
    raw = json.loads((_here() / "widgets.json").read_text(encoding="utf-8"))
    items = raw["widgets"] if isinstance(raw, dict) else raw
    out = []
    for it in items:
        out.append({
            "id": it["id"],
            "label": it.get("label", it["id"]),
            "desc": it.get("desc", ""),
            "group": it.get("group", "系统"),
            "default": bool(it.get("default", False)),
            "minWidth": int(it["minWidth"]),
            "minHeight": int(it["minHeight"]),
            "draw": it["draw"],
        })
    return out


REGISTRY = load_registry()
WMAP = {w["id"]: w for w in REGISTRY}

# ── 字体 ──────────────────────────────────────────────────────────
_FC = {}


def _load(size, kind):
    path = {"reg": FONT_REG, "bd": FONT_BD, "mono": FONT_MONO}[kind]
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def F(size, kind="reg", text=None):
    """取字体。text 里含非 ASCII（汉字等）时必须走 msyh，
    因为 consola.ttf 不含汉字，用它会渲染成方块。"""
    size = max(8, int(size))
    if text is not None and any(ord(c) > 127 for c in str(text)):
        kind = "bd" if kind == "bd" else "reg"
    key = (size, kind)
    f = _FC.get(key)
    if f is None:
        f = _FC[key] = _load(size, kind)
    return f


def _fs(h, frac, lo, hi):
    """按格子高度算字号。"""
    return max(lo, min(hi, int(h * frac)))


# ── 通用绘制工具 ──────────────────────────────────────────────────
def card(d, b, fill=None):
    if fill is None:
        fill = CARD
    x, y, w, h = b["x"], b["y"], b["w"], b["h"]
    d.rounded_rectangle([x, y, x + w - 1, y + h - 1], radius=5, fill=fill, outline=LINE)
    if w >= 24 and h >= 36:
        d.line((x + w - 17, y + 9, x + w - 8, y + 9), fill=LINE, width=2)


def cap(d, b, text):
    """格子左上角的部件标签。"""
    d.text((b["x"] + 8, b["y"] + 6), str(text), font=F(_fs(b["h"], 0.17, 9, 15), "bd", text), fill=DIM)


# Simple line marks stay distinct on the LCD and cost little to encode.
ICONS = {
    "time": (((6, 1), (10, 3), (11, 7), (9, 10), (5, 11), (2, 9), (1, 5), (3, 2), (6, 1)),
             ((6, 3), (6, 6), (8, 7))),
    "performance": (((2, 3), (10, 3), (10, 9), (2, 9), (2, 3)),
                    ((4, 1), (4, 3)), ((8, 9), (8, 11)), ((5, 6), (7, 6))),
    "network": (((1, 5), (4, 3), (8, 3), (11, 5)), ((3, 7), (6, 5), (9, 7)), ((5, 9), (7, 9))),
    "system": (((1, 3), (11, 3), (11, 9), (1, 9), (1, 3)), ((4, 6), (8, 6))),
}


def icon(d, x, y, kind, color):
    for line in ICONS[kind]:
        d.line([(x + dx, y + dy) for dx, dy in line], fill=color, width=1)


def bar(d, x, y, w, h, pct, color, track=None):
    if track is None:
        track = TRACK
    pct = 0.0 if pct is None else max(0.0, min(100.0, float(pct)))
    h = max(4, int(h))
    r = h // 2
    d.rounded_rectangle([x, y, x + w, y + h], radius=r, fill=track)
    fw = int((w - 2) * pct / 100.0)
    if fw > 0:
        d.rounded_rectangle([x + 1, y + 1, x + 1 + fw, y + h - 1], radius=max(1, r - 1), fill=color)


def pick(pct, warn=70, bad=88):
    if pct is None:
        return NA
    if pct >= bad:
        return BAD
    if pct >= warn:
        return WARN
    return GOOD


def temp_color(t):
    if t is None:
        return NA
    if t >= 80:
        return BAD
    if t >= 65:
        return WARN
    return GOOD


def num(v, nd=1, dash="n/a"):
    if v is None:
        return dash
    try:
        return f"{float(v):.{nd}f}"
    except Exception:
        return dash


def txt(v, dash="n/a"):
    if v is None:
        return dash
    s = str(v)
    return s if s != "" else dash


def put(d, xy, s, size, kind="mono", fill=None, anchor=None):
    if fill is None:
        fill = TXT
    d.text(xy, str(s), font=F(size, kind, s), fill=fill, anchor=anchor)


def tw(s, size, kind="mono"):
    """文本像素宽度。测量不出来就按 0.6em 估算，绝不让测量失败把绘图打断。"""
    s = str(s)
    try:
        return F(size, kind, s).getlength(s)
    except Exception:
        return len(s) * size * 0.6


def fitw(s, maxw, size, kind="mono", lo=8):
    """宽度装不下时先逐级缩小字号，仍装不下就截断加省略号。返回 (文本, 字号)。"""
    s = str(s)
    maxw = float(maxw)
    if maxw <= 1:
        return "", int(lo)
    for sz in range(int(size), int(lo) - 1, -1):
        if tw(s, sz, kind) <= maxw:
            return s, sz
    for n in range(len(s) - 1, 0, -1):
        cand = s[:n] + "…"
        if tw(cand, int(lo), kind) <= maxw:
            return cand, int(lo)
    return "", int(lo)


def _frame(d, b, label=None, fill=None):
    """画卡片 + 左上角标签，返回内容区 (cx, cy, cw, ch)。

    cy 从标签下沿之后才开始，数值一律限制在内容区内，
    这样即使布局把行高压到 26px，标签与数值也不会重叠。
    """
    card(d, b, fill)
    lab = _fs(b["h"], 0.17, 8, 15)
    if label:
        x, y, w = b["x"], b["y"], b["w"]
        group = WMAP.get(b.get("id"), {}).get("group")
        kind = {"时间": "time", "效率": "time", "性能": "performance", "网络": "network"}.get(group, "system")
        show_icon = b["h"] >= 43 and w >= 94
        inset = 24 if show_icon else 8
        if show_icon:
            icon(d, x + 8, y + 5 + (lab - 11) // 2, kind, ACCENT)
        heading, size = fitw(label, w - inset - 22, lab, "bd", 8)
        put(d, (x + inset, y + 4), heading, size, "bd", DIM)
    cx = b["x"] + 7
    cy = b["y"] + lab + 7
    cw = max(4, b["w"] - 14)
    ch = max(4, b["y"] + b["h"] - 5 - cy)
    return cx, cy, cw, ch


def _metric(d, b, label, value, unit, color, sub=None, pct=None, fill=None):
    """标准指标卡：标签 + 大数值 + 单位 + 可选副行 + 可选进度条。

    纵向预算全部按内容区高度 ch 分配，因此任意格子高度都不会重叠或越出卡片。
    数值优先：装不下时先缩小字号，仍装不下就整段去掉单位（百分比落到副行）。
    """
    cx, cy, cw, ch = _frame(d, b, label, fill)
    bar_h = max(4, int(ch * 0.18)) if (pct is not None and ch >= 26) else 0
    sub_h = max(9, int(ch * 0.22)) if (sub and ch >= 22) else 0
    inner = ch - bar_h - sub_h - (3 if (bar_h or sub_h) else 0)
    vs = max(9, min(52, int(max(9, inner) * 0.94)))
    us = max(8, int(vs * 0.48))
    val = value
    unit = unit or ""
    # 先试探：带单位时数值能不能保持在合理字号
    uw = tw(unit, us, "bd") if unit else 0
    room = cw - uw - (3 if unit else 0)
    if unit and tw(value, vs, "bd") > room and room < tw(value, max(9, int(vs * 0.8)), "bd"):
        # 单位挤掉了太多宽度，且缩到八折仍不够 → 去掉单位，宽度全给数值
        if sub:
            sub = sub + " " + unit
        unit, uw, room = "", 0, cw
    # 读不到的数（n/a）不配占满整个格子：用两档小的字号，换成中性灰。
    # 一个巨大的「n/a」比直接不显示还难看，而且会让人以为屏幕坏了。
    missing = str(value) in ("n/a", "-", "")
    if missing and not unit:
        vs = max(11, int(vs * 0.52))
        color = NA
    vtxt, vs = fitw(value, room, vs, "bd", 9)
    ymid = cy + max(9, inner) * 0.5
    put(d, (cx, ymid), vtxt, vs, "bd", color, anchor="lm")
    if unit:
        unit_x = min(cx + cw - uw, cx + tw(vtxt, vs, "bd") + 4)
        put(d, (unit_x, ymid), unit, us, "bd", color, anchor="lm")
    y = cy + max(9, inner)
    if sub_h:
        stxt, ssz = fitw(sub, cw, min(16, sub_h), "mono", 8)
        put(d, (cx, y + sub_h * 0.5), stxt, ssz, "mono", DIM, anchor="lm")
        y += sub_h + 3
    if bar_h:
        bar(d, cx, y, cw, bar_h, pct, color)
    return cx, cy, cw, ch


# ── 布局 ──────────────────────────────────────────────────────────
def layout(ids):
    """把部件 id 列表排进 480x360。返回 (boxes, info)。

    算法：先按 minWidth 货架式分行，再按 minHeight 分配每行高度，
    超高则整体压缩（有下限），有余量则按比例撑满，保证任何部件都不越界。
    """
    unknown = [i for i in ids if i not in WMAP]
    if unknown:
        raise UnknownWidget("未注册的部件 id: " + ", ".join(unknown))

    usable_w = W - 2 * MARGIN
    rows = []
    cur, curw = [], 0
    for wid in ids:
        mw = min(max(24, int(WMAP[wid]["minWidth"])), usable_w)
        add = mw if not cur else mw + GAP
        if cur and curw + add > usable_w:
            rows.append(cur)
            cur, curw = [wid], mw
        else:
            cur.append(wid)
            curw += add
    if cur:
        rows.append(cur)
    if not rows:
        return [], {"rows": [], "overflow": False, "scaled": False, "empty": True}

    # 每行宽度：把剩余宽度按 minWidth 比例分给该行部件
    widths = []
    for row in rows:
        base = [min(max(24, int(WMAP[i]["minWidth"])), usable_w) for i in row]
        gaps = GAP * (len(row) - 1)
        slack = usable_w - gaps - sum(base)
        if slack > 0 and sum(base) > 0:
            extra = [int(slack * b / sum(base)) for b in base]
            diff = slack - sum(extra)
            k = 0
            while diff > 0:
                extra[k % len(extra)] += 1
                diff -= 1
                k += 1
            widths.append([base[i] + extra[i] for i in range(len(base))])
        else:
            widths.append(base)

    avail = H - 2 * MARGIN
    base_h = [max(24, int(max(WMAP[i]["minHeight"] for i in row))) for row in rows]
    gaps_h = GAP * (len(rows) - 1)
    scaled = False

    if sum(base_h) + gaps_h > avail:
        scaled = True
        # 按「各行的 minHeight 超出下限的部分」等比缩放，而不是一轮一轮削最大值——
        # 后者会把所有行压成同一高度，矮行（如 ip/host 只要 44）跟着被削，
        # 而高行（时钟 96）也没得到应有的份额。
        floor = 26
        budget = max(1, avail - gaps_h)
        n = len(base_h)
        reducible = sum(base_h) - n * floor
        if reducible > 0:
            f = 1.0 - float(sum(base_h) - budget) / float(reducible)
            f = max(0.0, min(1.0, f))
        else:
            f = 0.0
        heights = [max(floor, int(floor + (b - floor) * f)) for b in base_h]
        # 取整后可能仍超，继续从当前最高的行往下削
        while sum(heights) + gaps_h > avail:
            idx = max(range(n), key=lambda i: heights[i])
            if heights[idx] <= floor:
                break
            heights[idx] -= 1
        if sum(heights) + gaps_h > avail:  # 连下限都放不下（行数极端多）才走整体缩放
            room = max(1, avail - gaps_h)
            s = room / float(sum(heights))
            heights = [max(1, int(h * s)) for h in heights]
    else:
        room = avail - gaps_h
        slack = room - sum(base_h)
        if slack > 0 and sum(base_h) > 0:
            extra = [int(slack * b / sum(base_h)) for b in base_h]
            diff = slack - sum(extra)
            k = 0
            while diff > 0:
                extra[k % len(extra)] += 1
                diff -= 1
                k += 1
            heights = [base_h[i] + extra[i] for i in range(len(base_h))]
        else:
            heights = base_h

    boxes = []
    y = MARGIN
    for ri, row in enumerate(rows):
        x = MARGIN
        hh = heights[ri]
        for i, wid in enumerate(row):
            ww = widths[ri][i]
            boxes.append({"id": wid, "x": int(x), "y": int(y), "w": int(ww), "h": int(hh)})
            x += ww + GAP
        y += hh + GAP

    overflow = any(b["x"] < 0 or b["y"] < 0 or b["x"] + b["w"] > W or b["y"] + b["h"] > H for b in boxes)
    info = {"rows": [[i for i in r] for r in rows], "overflow": overflow, "scaled": scaled, "empty": False}
    return boxes, info


def paginate(ids):
    """Keep each page at natural minimum heights; never shrink fifteen tiles to fit.

    Uses the same layout solver as rendering, so gallery order is screen order.
    Empty selections stay empty instead of silently restoring a preset.
    """
    pages, current = [], []
    for wid in ids:
        candidate = current + [wid]
        boxes, info = layout(candidate)
        if current and (info['scaled'] or info['overflow'] or len(candidate) > 6):
            pages.append(current)
            current = [wid]
        else:
            current = candidate
    if current:
        pages.append(current)
    return pages


def resolve_widgets(widgets=None, preset="default"):
    """--widgets 优先于 --preset。两者都没给就用 default 预设。"""
    if widgets is not None:
        ids = [w.strip() for w in widgets if str(w).strip()]
        return ids
    if preset not in PRESETS:
        raise ValueError("未知预设: %s（可用: %s）" % (preset, ", ".join(sorted(PRESETS))))
    return list(PRESETS[preset])


# ── 各部件绘制 ────────────────────────────────────────────────────
def draw_clock_big(d, b, st, ctx):
    cx, cy, cw, ch = _frame(d, b, "时间", CARD_HI)
    vs = max(12, min(72, int(ch * 0.88)))
    vtxt, vs = fitw(txt(st.get("clock"), "--:--:--"), cw, vs, "bd", 14)
    put(d, (cx + 3, cy + ch / 2), vtxt, vs, "bd", ACCENT, anchor="lm")


def draw_clock_utc(d, b, st, ctx):
    _metric(d, b, "世界时间", txt(st.get("clock_utc")), "UTC", ACCENT)


def day_progress(clock):
    try:
        current = dt.time.fromisoformat(clock)
    except (TypeError, ValueError):
        return None
    return (current.hour * 60 + current.minute) * 100 // 1440


def draw_day_progress(d, b, st, ctx):
    p = day_progress(st.get("clock"))
    _metric(d, b, "今日进度", num(p, 0), "%" if p is not None else "", ACCENT, pct=p)


def draw_mem_available(d, b, st, ctx):
    available = st.get("mem_available_gb")
    _metric(d, b, "可用内存", num(available, 1), "GB" if available is not None else "", TXT)


def format_focus(seconds):
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
        return None
    minutes, remaining = divmod(int(seconds), 60)
    return f"{minutes:02d}:{remaining:02d}"


def draw_focus_timer(d, b, st, ctx):
    clock = format_focus(st.get("focus_remaining"))
    detail = "从桌面应用启动" if clock is None else ("时间到" if clock == "00:00" else "剩余时间")
    _metric(d, b, "专注倒计时", clock or "未启动", "", ACCENT, sub=detail, fill=CARD_HI)


WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def date_label(raw):
    """把 2026-11-05 补成 2026-11-05 周四。已经带星期的原样返回。

    采样器与桌面程序各自给出日期串，两者格式都是 ISO；星期只有一处能算准，
    所以放在绘制这一层，谁传进来都一样。
    """
    s = str(raw or "").strip()
    if not s or s == "n/a":
        return "n/a"
    if "周" in s or "星期" in s:
        return s
    try:
        y, m, day = (int(x) for x in s.split("-")[:3])
        return "%s %s" % (s, WEEKDAYS[dt.date(y, m, day).weekday()])
    except (ValueError, TypeError):
        return s


def draw_date(d, b, st, ctx):
    cx, cy, cw, ch = _frame(d, b, "日期")
    label = date_label(st.get("date"))
    vs = max(10, min(30, int(ch * 0.86)))
    vtxt, vs = fitw(label, cw, vs, "mono", 10)
    # 日期跟着星期变长，居中会显得偏；左对齐配卡片留白更稳。
    put(d, (cx, cy + ch / 2), vtxt, vs, "mono", TXT, anchor="lm")


def draw_cpu(d, b, st, ctx):
    p = st.get("cpu_load")
    c = pick(p)
    fq = st.get("cpu_freq_ghz")
    _metric(d, b, "CPU", num(p, 1), "%", c,
            sub=("%s GHz" % num(fq, 2)) if fq is not None else "n/a", pct=p)


def draw_mem(d, b, st, ctx):
    p = st.get("mem_pct")
    c = pick(p, 75, 92)
    sub = "%s / %s GB" % (num(st.get("mem_used_gb"), 1), num(st.get("mem_total_gb"), 1))
    _metric(d, b, "内存", num(p, 1), "%", c, sub=sub, pct=p)


def draw_gpu(d, b, st, ctx):
    g = st.get("gpu_pct")
    _metric(d, b, "GPU", num(g, 0), "%" if g is not None else "", pick(g), pct=g)


def draw_temp(d, b, st, ctx):
    t = st.get("temp_c")
    _metric(d, b, "温度", num(t, 1), "°C" if t is not None else "", temp_color(t), pct=t)


def draw_disk(d, b, st, ctx):
    _metric(d, b, "磁盘剩余", num(st.get("disk_free_gb"), 1), "GB", TXT)


def draw_net(d, b, st, ctx):
    cx, cy, cw, ch = _frame(d, b, "网络")
    dn, up = st.get("net_down_mbps"), st.get("net_up_mbps")
    half = ch / 2.0
    ds = max(10, min(24, int(half * 0.86)))
    dtxt, ds = fitw("↓ %s" % num(dn, 2), cw, ds, "mono", 10)
    put(d, (cx, cy + half * 0.5), dtxt, ds, "mono", GOOD, anchor="lm")
    us = max(8, min(14, int(half * 0.62)))
    utxt, us = fitw("↑ %s Mbps" % num(up, 2), cw, us, "mono", 8)
    put(d, (cx, cy + half * 1.5), utxt, us, "mono", ACCENT, anchor="lm")


def draw_battery(d, b, st, ctx):
    p = st.get("bat_pct")
    if p is None:
        # 拿不到电量（台式机或电池驱动没暴露）时，不要用一个巨大的 AC 占满卡片。
        # 说明「在供电」就够了，字小一点，把视觉重量让给真正有数的部件。
        cx, cy, cw, ch = _frame(d, b, "电池")
        tag = "交流供电" if st.get("bat_ac") else "未检测到电池"
        vs = max(10, min(15, int(ch * 0.30)))
        vtxt, vs = fitw(tag, cw, vs, "bd", 10)
        put(d, (cx, cy + ch / 2), vtxt, vs, "bd", NA, anchor="lm")
        return
    c = GOOD if p > 30 else (WARN if p > 15 else BAD)
    tag = "充电中" if st.get("bat_charging") else ("交流供电" if st.get("bat_ac") else "放电中")
    _metric(d, b, "电池", num(p, 0), "%", c, sub=tag, pct=p)


def draw_uptime(d, b, st, ctx):
    cx, cy, cw, ch = _frame(d, b, "已运行")
    vs = max(10, min(26, int(ch * 0.84)))
    vtxt, vs = fitw(txt(st.get("uptime")), cw, vs, "mono", 10)
    put(d, (cx, cy + ch / 2), vtxt, vs, "mono", TXT, anchor="lm")


def draw_volume(d, b, st, ctx):
    v = st.get("vol_pct")
    _metric(d, b, "音量", num(v, 0), "%" if v is not None else "", ACCENT if v is not None else NA, pct=v)


def draw_host(d, b, st, ctx):
    cx, cy, cw, ch = _frame(d, b, "主机")
    vs = max(9, min(22, int(ch * 0.80)))
    vtxt, vs = fitw(txt(st.get("host")), cw, vs, "mono", 9)
    put(d, (cx, cy + ch / 2), vtxt, vs, "mono", TXT, anchor="lm")


def draw_ip(d, b, st, ctx):
    cx, cy, cw, ch = _frame(d, b, "IP")
    vs = max(9, min(22, int(ch * 0.80)))
    vtxt, vs = fitw(txt(st.get("ip")), cw, vs, "mono", 9)
    put(d, (cx, cy + ch / 2), vtxt, vs, "mono", TXT, anchor="lm")


def draw_top_proc(d, b, st, ctx):
    cx, cy, cw, ch = _frame(d, b, "最占进程")
    name = txt(st.get("top_proc"))
    p = st.get("top_proc_pct")
    line2 = ch >= 30  # 高格子才分两行，矮格子挤一行
    if line2:
        top = ch * 0.58
        ns = max(10, min(24, int(top * 0.82)))
        ntxt, ns = fitw(name, cw, ns, "mono", 10)
        put(d, (cx, cy + top * 0.5), ntxt, ns, "mono", TXT, anchor="lm")
        ps = max(8, min(14, int((ch - top) * 0.72)))
        ptxt = ("%s%%" % num(p, 1)) if p is not None else "n/a"
        ptxt, ps = fitw(ptxt, cw, ps, "mono", 8)
        put(d, (cx, cy + top + (ch - top) * 0.5), ptxt, ps, "mono", pick(p), anchor="lm")
    else:
        ptxt = ("%s%%" % num(p, 1)) if p is not None else "n/a"
        ps = max(8, min(14, int(ch * 0.62)))
        pw = tw(ptxt, ps, "mono")
        ns = max(9, min(24, int(ch * 0.80)))
        ntxt, ns = fitw(name, cw - pw - 6, ns, "mono", 9)
        put(d, (cx, cy + ch / 2), ntxt, ns, "mono", TXT, anchor="lm")
        put(d, (cx + cw, cy + ch / 2), ptxt, ps, "mono", pick(p), anchor="rm")


def draw_history_cpu(d, b, st, ctx):
    cx, cy, cw, ch = _frame(d, b, "CPU 历史")
    hist = (ctx or {}).get("history") or []
    if cw < 8 or ch < 10:
        return
    y0 = cy
    hh = ch
    d.line([cx, y0 + hh, cx + cw, y0 + hh], fill=LINE)
    if len(hist) < 2:
        put(d, (cx + cw / 2, y0 + hh / 2), "等待更多采样",
            max(8, min(13, int(ch * 0.5))), "reg", NA, anchor="mm")
        return
    vals = [h.get("cpu") for h in hist]
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        put(d, (cx + cw / 2, y0 + hh / 2), "n/a", max(9, min(16, int(ch * 0.6))), "mono", NA, anchor="mm")
        return
    n = len(vals)
    step = cw / float(n - 1)
    pts = [(cx + i * step, y0 + hh - (max(0.0, min(100.0, float(v))) / 100.0) * hh) for i, v in enumerate(vals)]
    d.line(pts, fill=ACCENT, width=2, joint="curve")
    cur = vals[-1]
    put(d, (cx + cw, cy - 2), "%s%%" % num(cur, 0), _fs(b["h"], 0.17, 8, 15), "bd", pick(cur), anchor="ra")



def _text_rows(d, b, title, rows, color=None):
    if color is None:
        color = TXT
    cx, cy, cw, ch = _frame(d, b, title)
    step = ch / max(1, len(rows))
    for index, row in enumerate(rows):
        size = min(26 if index == 0 else 14, max(9, int(step * 0.75)))
        text, size = fitw(str(row), cw, size, 'bd' if index == 0 else 'reg', 8)
        put(d, (cx, cy + step * (index + 0.5)), text, size,
            'bd' if index == 0 else 'reg', color if index == 0 else DIM, anchor='lm')


def draw_weather(d, b, st, ctx):
    value = st.get('weather') or {}
    if not isinstance(value, dict):
        value = {}
    temperature = value.get('temperature')
    status = value.get('status')
    main = (num(temperature, 1) + ' °C  ' + str(value.get('label', ''))) if temperature is not None else value.get('label', '请选择城市')
    observed = str(value.get('observed_at', '')).replace('T', ' ')
    detail = ('缓存 · ' if status == 'stale' else '') + (observed or '在组件设置中选择城市')
    _text_rows(d, b, '天气', [main, value.get('city', '尚未设置城市'), detail, 'Open-Meteo · CC BY 4.0'], WARN if status == 'stale' else ACCENT)


def draw_codex(d, b, st, ctx):
    value = st.get('codex') or {}
    if not isinstance(value, dict):
        value = {}
    age = value.get('age')
    detail = f'{int(age) // 60} 分钟前更新' if isinstance(age, (int, float)) and age >= 60 else ('刚刚更新' if age is not None else '只读本机任务状态')
    state = value.get('state')
    color = GOOD if state == 'completed' else WARN if state in ('waiting', 'unknown') else ACCENT
    _text_rows(d, b, 'Codex', [value.get('label', '未发现本地任务'), detail,
                             value.get('source', '本地事件') + ' · 无事件不代表完成'], color)


def draw_playing(d, b, st, ctx):
    """正在播放：歌名 + 歌手 + 进度条。没有任何播放器时说明「未在播放」。"""
    value = st.get('media') or {}
    if not isinstance(value, dict):
        value = {}
    cx, cy, cw, ch = _frame(d, b, '正在播放')
    title = str(value.get('title') or '').strip()
    artist = str(value.get('artist') or '').strip()
    status = str(value.get('status') or 'none').lower()
    if not title:
        put(d, (cx + cw / 2, cy + ch / 2), '未在播放', max(10, min(22, int(ch * 0.42))),
            'bd', NA, anchor='mm')
        return
    # 标题占上半，歌手 + 进度占下半。标题可能很长（日文/英文混排），交给 fitw 缩。
    ts = max(11, min(30, int(ch * 0.34)))
    ttxt, ts = fitw(title, cw, ts, 'bd', 11)
    put(d, (cx, cy + ch * 0.26), ttxt, ts, 'bd', ACCENT, anchor='lm')
    sub = artist or ('播放中' if status == 'playing' else '已暂停')
    ss = max(9, min(16, int(ch * 0.18)))
    stxt, ss = fitw(sub, cw, ss, 'reg', 9)
    put(d, (cx, cy + ch * 0.66), stxt, ss, 'reg', DIM, anchor='lm')
    pos, dur = value.get('position'), value.get('duration')
    if isinstance(pos, (int, float)) and isinstance(dur, (int, float)) and dur > 0:
        frac = max(0.0, min(1.0, float(pos) / float(dur)))
        bar(d, cx, cy + ch - 5, cw, 4, frac * 100.0,
            GOOD if status == 'playing' else WARN)
        left = int(dur - pos)
        stamp = '%d:%02d / %d:%02d' % (int(pos) // 60, int(pos) % 60,
                                      int(dur) // 60, int(dur) % 60)
        cs = max(8, min(11, int(ch * 0.13)))
        put(d, (cx + cw, cy + ch - 8), stamp, cs, 'mono',
            GOOD if left > 0 else NA, anchor='rs')


def draw_calendar(d, b, st, ctx):
    """日历：今天几号（大字）+ 星期 + 年内第几周。日期来自 state 的 date/clock。"""
    cx, cy, cw, ch = _frame(d, b, '日历')
    label = date_label(st.get('date'))
    raw = str(st.get('date') or '').strip()
    day = ''
    weekday = ''
    for part in label.split():
        if part.startswith('周') or part.startswith('星期'):
            weekday = part
    # 日期串是 2026-11-06，最后一段才是「几号」；直接按 isdigit 找会把整串
    # 「2026-11-06」当成候选而全部落空，之前就在界面上显示成两个横杠。
    pieces = raw.split('-')
    if len(pieces) >= 3 and pieces[2].isdigit():
        day = str(int(pieces[2]))
    ds = max(18, min(64, int(ch * 0.72)))
    dtxt, ds = fitw(day or '--', cw * 0.5, ds, 'bd', 18)
    put(d, (cx, cy + ch * 0.5), dtxt, ds, 'bd', ACCENT, anchor='lm')
    right = cx + cw * 0.52
    ws = max(10, min(20, int(ch * 0.30)))
    put(d, (right, cy + ch * 0.32), weekday or 'n/a', ws, 'bd', TXT, anchor='lm')
    week = ''
    try:
        y, m, dnum = (int(x) for x in raw.split('-')[:3])
        week = '第 %d 周' % dt.date(y, m, dnum).isocalendar()[1]
    except (ValueError, TypeError):
        week = ''
    es = max(8, min(13, int(ch * 0.19)))
    put(d, (right, cy + ch * 0.72), week or (label if label != 'n/a' else ''), es, 'mono',
        DIM, anchor='lm')


def link_label(ms, loss, wired):
    """把连接状态算成 (数值, 单位, 副标题, 颜色)。

    纯函数，方便测试：没有延时就是 ("n/a", "", ...)，绝不用 0 冒充。
    """
    mode = "有线 USB" if wired is True else ("2.4G 无线" if wired is False else "n/a")
    if ms is None:
        value, unit, color = "n/a", "", NA
    else:
        value = "%.0f" % float(ms)
        unit = "ms"
        color = GOOD if float(ms) < 40 else (WARN if float(ms) < 120 else BAD)
    if loss is None:
        sub = mode
    else:
        sub = "%s   丢 %d%%" % (mode, int(round(float(loss))))
    return value, unit, sub, color


def draw_link(d, b, st, ctx):
    """连接方式 + 延迟 + 丢包。

    键盘用 USB 线连着就是「有线」，否则按 2.4G 显示。延迟来自 ping 采样，
    拿不到就写 n/a，不编造一个好看的数。
    """
    _frame(d, b, "连接")
    value, unit, sub, color = link_label(st.get("ping_ms"), st.get("ping_loss"),
                                        st.get("link_wired"))
    _metric(d, b, "", value, unit, color, sub=sub)


def draw_net_pulse(d, b, st, ctx):
    """网速脉冲：下行/上行各一条横向条，用相对刻度（300 Mbps 满格）。

    和 draw_net 的区别：那个只报数字，这个给一眼就能看出「现在有没有在跑」的
    条形，适合放在小尺寸格子里。
    """
    cx, cy, cw, ch = _frame(d, b, '网速')
    SCALE = 300.0
    dn = st.get('net_down_mbps')
    up = st.get('net_up_mbps')
    half = ch / 2.0
    for index, (val, tag, color) in enumerate(((dn, '↓', GOOD), (up, '↑', ACCENT))):
        y = cy + half * (index + 0.5)
        vs = max(9, min(16, int(half * 0.44)))
        label = '%s %s' % (tag, num(val, 2))
        put(d, (cx, y - vs * 0.55), label, vs, 'mono', color, anchor='lm')
        frac = 0.0
        if isinstance(val, (int, float)) and val > 0:
            frac = min(1.0, float(val) / SCALE)
        bh = max(3, int(half * 0.26))
        bar(d, cx, y + vs * 0.18, cw, bh, frac * 100.0, color)


DRAW_FNS = {
    "draw_focus_timer": draw_focus_timer,
    "draw_clock_utc": draw_clock_utc,
    "draw_day_progress": draw_day_progress,
    "draw_mem_available": draw_mem_available,
    "draw_weather": draw_weather,
    "draw_codex": draw_codex,
    "draw_clock_big": draw_clock_big,
    "draw_date": draw_date,
    "draw_cpu": draw_cpu,
    "draw_mem": draw_mem,
    "draw_gpu": draw_gpu,
    "draw_temp": draw_temp,
    "draw_disk": draw_disk,
    "draw_net": draw_net,
    "draw_battery": draw_battery,
    "draw_uptime": draw_uptime,
    "draw_volume": draw_volume,
    "draw_host": draw_host,
    "draw_ip": draw_ip,
    "draw_top_proc": draw_top_proc,
    "draw_history_cpu": draw_history_cpu,
    "draw_playing": draw_playing,
    "draw_calendar": draw_calendar,
    "draw_net_pulse": draw_net_pulse,
    "draw_link": draw_link,
}


# ── 历史采样（供 history_cpu 用） ─────────────────────────────────
def load_history(state_dir):
    if not state_dir:
        return []
    p = Path(state_dir) / "history.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def append_history(state_dir, st):
    """把本次的 cpu/mem 采样追加进 DIR/history.json，只保留最近 60 条。"""
    if not state_dir:
        return []
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    hist = load_history(state_dir)
    hist.append({
        "t": st.get("clock"),
        "cpu": st.get("cpu_load"),
        "mem": st.get("mem_pct"),
    })
    hist = hist[-HISTORY_KEEP:]
    (d / "history.json").write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")
    return hist


# ── 渲染主流程 ────────────────────────────────────────────────────
def render(st, widgets=None, preset="default", quality=72, limit=None, history=None,
           theme=None):
    """返回 (jpeg_bytes, PIL.Image)。st 里缺失/null 的字段一律画 n/a，不崩。

    theme 决定彩色皮肤；None 表示沿用当前全局。
    """
    if not isinstance(st, dict):
        st = {}
    if theme:
        apply_theme(theme)
    ids = resolve_widgets(widgets, preset)
    boxes, info = layout(ids)

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    # 顶部一条极细的强调线，避免整体过暗
    d.rectangle([MARGIN, 0, W - MARGIN - 1, 1], fill=LINE)
    # 右下角原本印着分辨率，那是开发时确认画布用的，用户看不懂也不需要，
    # 还会在浅色主题上显得脏。删掉。

    ctx = {"history": history or [], "info": info}
    for b in boxes:
        fn = DRAW_FNS.get(WMAP[b["id"]]["draw"])
        if fn is None:
            raise UnknownWidget("部件 %s 的 draw 函数 %s 未实现" % (b["id"], WMAP[b["id"]]["draw"]))
        try:
            fn(d, b, st, ctx)
        except Exception as exc:  # 单个部件画挂不能拖垮整张图
            card(d, b)
            cap(d, b, WMAP[b["id"]]["label"])
            put(d, (b["x"] + 8, b["y"] + b["h"] * 0.7), "n/a", 14, "mono", NA, anchor="lm")
            if "--debug" in sys.argv:
                sys.stderr.write("[widget %s] %r\n" % (b["id"], exc))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=int(quality), optimize=True, progressive=False)
    data = buf.getvalue()

    if limit and len(data) > limit:
        # 二分降质量，尽量贴近上限
        lo, hi, best = 20, int(quality), data
        for _ in range(8):
            mid = (lo + hi) // 2
            b2 = io.BytesIO()
            img.save(b2, format="JPEG", quality=mid, optimize=True, progressive=False)
            cand = b2.getvalue()
            if len(cand) <= limit:
                best, lo = cand, mid
            else:
                hi = mid
        data = best

    return data, img


def registry_json():
    return {
        "canvas": {"width": W, "height": H, "format": "baseline-jpeg"},
        "presets": PRESETS,
        "themes": [
            {"id": k, "label": THEMES[k]["label"], "blurb": THEMES[k]["blurb"],
             "light": THEMES[k]["light"],
             "swatch": "rgb(%d,%d,%d)" % THEMES[k]["BG"],
             "swatch2": "rgb(%d,%d,%d)" % THEMES[k]["ACCENT"]}
            for k in THEME_NAMES
        ],
        "theme": THEME,
        "groups": sorted({w["group"] for w in REGISTRY}),
        "widgets": [
            {
                "id": w["id"],
                "label": w["label"],
                "desc": w["desc"],
                "group": w["group"],
                "default": w["default"],
                "minWidth": w["minWidth"],
                "minHeight": w["minHeight"],
                "draw": w["draw"],
                "implemented": w["draw"] in DRAW_FNS,
            }
            for w in REGISTRY
        ],
    }


def native_background(skin):
    """Dark static wallpaper under the existing four white firmware labels."""
    palettes = {"graphite": ((15, 20, 28), (26, 35, 48), (104, 172, 225)),
                "tide": ((13, 33, 37), (20, 50, 55), (118, 220, 191))}
    bg, panel, ink = palettes[skin]
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    put(d, (20, 6), "NEOS75", 14, "bd", ink)
    put(d, (460, 7), "LIVE", 12, "mono", ink, anchor="ra")
    for row in range(4):
        y = 28 + 70 * row
        d.rounded_rectangle((10, y, 469, y + 62), 5, fill=panel)
        d.line((10, y + 10, 10, y + 52), fill=ink, width=2)
    d.line((20, 317, 460, 317), fill=panel, width=2)
    for x, kind in zip(range(20, 420, 20), ICONS):
        icon(d, x, 331, kind, ink)
    put(d, (460, 332), "HOST / USB", 13, "mono", ink, anchor="ra")
    return img


def main(argv=None):
    ap = argparse.ArgumentParser(description="状态 JSON → 480x360 baseline JPEG")
    ap.add_argument("--in", dest="inp", default=None, help="状态 JSON 路径（- 表示 stdin）")
    ap.add_argument("--out", dest="out", default=None, help="输出 JPEG 路径")
    ap.add_argument("--widgets", default=None, help="逗号分隔的部件 id，优先于 --preset")
    ap.add_argument("--preset", default="default", choices=sorted(PRESETS))
    ap.add_argument("--quality", type=int, default=72)
    ap.add_argument("--limit", type=int, default=None, help="字节上限，超出则二分降质量")
    ap.add_argument("--theme", default=None, choices=list(THEME_NAMES),
                    help="彩色看板皮肤（--list 查看色样）")
    ap.add_argument("--native-base", choices=("graphite", "tide"),
                    help="生成四行白字下的静态 JPEG 底图，需要 --out")
    ap.add_argument("--preview", default=None, help="另存一张 PNG 预览")
    ap.add_argument("--state-dir", dest="state_dir", default=None, help="历史采样目录（供 history_cpu）")
    ap.add_argument("--list", action="store_true", help="打印部件注册表 JSON 后退出")
    ap.add_argument("--layout", action="store_true", help="打印布局盒子 JSON 后退出")
    a = ap.parse_args(argv)

    if a.theme:
        apply_theme(a.theme)

    if a.native_base:
        if not a.out:
            ap.error("--native-base 需要 --out")
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        native_background(a.native_base).save(out, format="JPEG", quality=82, optimize=True, progressive=False)
        return 0

    if a.list:
        print(json.dumps(registry_json(), ensure_ascii=False, indent=2))
        return 0

    if a.layout:
        ids = resolve_widgets(a.widgets.split(",") if a.widgets else None, a.preset)
        boxes, info = layout(ids)
        print(json.dumps({"boxes": boxes, "info": info, "canvas": {"width": W, "height": H}},
                         ensure_ascii=False, indent=2))
        return 0

    if not a.inp or not a.out:
        ap.error("--in 与 --out 必填（或用 --list / --layout）")

    raw = sys.stdin.read() if a.inp == "-" else Path(a.inp).read_text(encoding="utf-8")
    try:
        st = json.loads(raw)
    except Exception as exc:
        sys.stderr.write("状态 JSON 解析失败: %s\n" % exc)
        return 2
    if not isinstance(st, dict):
        st = {}

    hist = append_history(a.state_dir, st) if a.state_dir else load_history(a.state_dir)

    try:
        data, img = render(st, a.widgets.split(",") if a.widgets else None, a.preset,
                           a.quality, a.limit, hist, a.theme)
    except UnknownWidget as exc:
        sys.stderr.write("错误: %s\n" % exc)
        return 3

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_bytes(data)
    if a.preview:
        Path(a.preview).parent.mkdir(parents=True, exist_ok=True)
        img.save(a.preview)

    print("✔ %s  %d B  (%dx%d, quality=%d, widgets=%d)%s" % (
        a.out, len(data), W, H, a.quality,
        len(resolve_widgets(a.widgets.split(",") if a.widgets else None, a.preset)),
        "" if not a.limit or len(data) <= a.limit else " [仍超上限]"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
