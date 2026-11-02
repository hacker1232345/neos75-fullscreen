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
import io
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:  # 保证中文 JSON 输出不炸
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
W, H = 480, 360
MARGIN = 6
GAP = 4

# 配色（深色底，高对比）
BG = (12, 13, 18)
CARD = (26, 29, 38)
CARD_HI = (34, 38, 50)
TRACK = (44, 48, 60)
LINE = (40, 44, 56)
TXT = (236, 239, 245)
DIM = (130, 140, 158)
FAINT = (78, 86, 102)
ACCENT = (94, 200, 255)
GOOD = (86, 214, 139)
WARN = (245, 191, 79)
BAD = (240, 92, 92)
NA = (96, 104, 120)

FONT_REG = r"C:\Windows\Fonts\msyh.ttc"
FONT_BD = r"C:\Windows\Fonts\msyhbd.ttc"
FONT_MONO = r"C:\Windows\Fonts\consola.ttf"

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
        "uptime",
    ],
    "performance": ["cpu", "mem", "gpu", "temp", "history_cpu", "top_proc", "uptime", "clock_big"],
    "network": ["clock_big", "net", "ip", "host", "uptime"],
}


class UnknownWidget(ValueError):
    """请求了 widgets.json 里不存在的部件 id。"""


def load_registry():
    """读取同目录 widgets.json（顶层是部件对象数组）。"""
    raw = json.loads((HERE / "widgets.json").read_text(encoding="utf-8"))
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
def card(d, b, fill=CARD):
    d.rounded_rectangle([b["x"], b["y"], b["x"] + b["w"] - 1, b["y"] + b["h"] - 1], radius=7, fill=fill)


def cap(d, b, text):
    """格子左上角的部件标签。"""
    d.text((b["x"] + 8, b["y"] + 6), str(text), font=F(_fs(b["h"], 0.17, 9, 15), "bd", text), fill=DIM)


def bar(d, x, y, w, h, pct, color, track=TRACK):
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


def put(d, xy, s, size, kind="mono", fill=TXT, anchor=None):
    d.text(xy, str(s), font=F(size, kind, s), fill=fill, anchor=anchor)


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
        heights = list(base_h)
        floor = 26
        while sum(heights) + gaps_h > avail:
            idx = max(range(len(heights)), key=lambda i: heights[i])
            if heights[idx] <= floor:
                break
            heights[idx] -= 1
        if sum(heights) + gaps_h > avail:
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


def resolve_widgets(widgets=None, preset="default"):
    """--widgets 优先于 --preset。两者都没给就用 default 预设。"""
    if widgets:
        ids = [w.strip() for w in widgets if str(w).strip()]
        return ids
    if preset not in PRESETS:
        raise ValueError("未知预设: %s（可用: %s）" % (preset, ", ".join(sorted(PRESETS))))
    return list(PRESETS[preset])


# ── 各部件绘制 ────────────────────────────────────────────────────
def draw_clock_big(d, b, st, ctx):
    card(d, b, CARD_HI)
    v = txt(st.get("clock"), "--:--:--")
    put(d, (b["x"] + b["w"] / 2, b["y"] + b["h"] * 0.58), v, _fs(b["h"], 0.40, 14, 54), "bd", ACCENT, anchor="mm")
    put(d, (b["x"] + 8, b["y"] + 6), "时间", _fs(b["h"], 0.17, 9, 15), "bd", DIM)


def draw_date(d, b, st, ctx):
    card(d, b)
    cap(d, b, "日期")
    v = txt(st.get("date"))
    put(d, (b["x"] + b["w"] / 2, b["y"] + b["h"] * 0.65), v, _fs(b["h"], 0.34, 11, 30), "mono", TXT, anchor="mm")


def draw_cpu(d, b, st, ctx):
    card(d, b)
    cap(d, b, "CPU")
    p = st.get("cpu_load")
    c = pick(p)
    put(d, (b["x"] + 8, b["y"] + b["h"] * 0.40), num(p, 1), _fs(b["h"], 0.40, 14, 40), "bd", c, anchor="lm")
    put(d, (b["x"] + b["w"] - 8, b["y"] + b["h"] * 0.40), "%", _fs(b["h"], 0.20, 10, 18), "bd", c, anchor="rm")
    fq = st.get("cpu_freq_ghz")
    put(d, (b["x"] + 8, b["y"] + b["h"] - 10), ("%s GHz" % num(fq, 2)) if fq is not None else "n/a",
        _fs(b["h"], 0.15, 9, 13), "mono", DIM, anchor="ls")
    bar(d, b["x"] + 8, b["y"] + b["h"] * 0.62, b["w"] - 16, max(5, int(b["h"] * 0.10)), p, c)


def draw_mem(d, b, st, ctx):
    card(d, b)
    cap(d, b, "内存")
    p = st.get("mem_pct")
    c = pick(p, 75, 92)
    put(d, (b["x"] + 8, b["y"] + b["h"] * 0.40), num(p, 1), _fs(b["h"], 0.40, 14, 40), "bd", c, anchor="lm")
    put(d, (b["x"] + b["w"] - 8, b["y"] + b["h"] * 0.40), "%", _fs(b["h"], 0.20, 10, 18), "bd", c, anchor="rm")
    put(d, (b["x"] + 8, b["y"] + b["h"] - 10), "%s / %s GB" % (num(st.get("mem_used_gb"), 1), num(st.get("mem_total_gb"), 1)),
        _fs(b["h"], 0.15, 9, 13), "mono", DIM, anchor="ls")
    bar(d, b["x"] + 8, b["y"] + b["h"] * 0.62, b["w"] - 16, max(5, int(b["h"] * 0.10)), p, c)


def draw_gpu(d, b, st, ctx):
    card(d, b)
    cap(d, b, "GPU")
    g = st.get("gpu_pct")
    c = pick(g)
    put(d, (b["x"] + 8, b["y"] + b["h"] * 0.62), num(g, 0), _fs(b["h"], 0.40, 14, 40), "bd", c, anchor="lm")
    if g is not None:
        put(d, (b["x"] + b["w"] - 8, b["y"] + b["h"] * 0.62), "%", _fs(b["h"], 0.20, 10, 18), "bd", c, anchor="rm")


def draw_temp(d, b, st, ctx):
    card(d, b)
    cap(d, b, "温度")
    t = st.get("temp_c")
    c = temp_color(t)
    put(d, (b["x"] + 8, b["y"] + b["h"] * 0.62), num(t, 1), _fs(b["h"], 0.40, 14, 40), "bd", c, anchor="lm")
    put(d, (b["x"] + b["w"] - 8, b["y"] + b["h"] * 0.62), "°C" if t is not None else "", _fs(b["h"], 0.20, 10, 18), "bd", c, anchor="rm")


def draw_disk(d, b, st, ctx):
    card(d, b)
    cap(d, b, "磁盘剩余")
    v = st.get("disk_free_gb")
    put(d, (b["x"] + 8, b["y"] + b["h"] * 0.62), num(v, 1), _fs(b["h"], 0.38, 13, 36), "bd", TXT, anchor="lm")
    put(d, (b["x"] + b["w"] - 8, b["y"] + b["h"] * 0.62), "GB", _fs(b["h"], 0.18, 10, 16), "bd", DIM, anchor="rm")


def draw_net(d, b, st, ctx):
    card(d, b)
    cap(d, b, "网络")
    dn, up = st.get("net_down_mbps"), st.get("net_up_mbps")
    fs = _fs(b["h"], 0.26, 11, 24)
    put(d, (b["x"] + 8, b["y"] + b["h"] * 0.52), "↓ %s" % num(dn, 2), fs, "mono", GOOD, anchor="lm")
    put(d, (b["x"] + 8, b["y"] + b["h"] * 0.80), "↑ %s Mbps" % num(up, 2), _fs(b["h"], 0.16, 9, 14), "mono", ACCENT, anchor="lm")


def draw_battery(d, b, st, ctx):
    card(d, b)
    cap(d, b, "电池")
    p = st.get("bat_pct")
    if p is None:
        put(d, (b["x"] + b["w"] / 2, b["y"] + b["h"] * 0.60), "AC", _fs(b["h"], 0.36, 14, 34), "bd", NA, anchor="mm")
        put(d, (b["x"] + 8, b["y"] + b["h"] - 10), "无电池 / 台式机", _fs(b["h"], 0.15, 9, 13), "reg", DIM, anchor="ls")
        return
    c = GOOD if p > 30 else (WARN if p > 15 else BAD)
    put(d, (b["x"] + 8, b["y"] + b["h"] * 0.42), num(p, 0), _fs(b["h"], 0.40, 14, 40), "bd", c, anchor="lm")
    put(d, (b["x"] + b["w"] - 8, b["y"] + b["h"] * 0.42), "%", _fs(b["h"], 0.20, 10, 18), "bd", c, anchor="rm")
    if st.get("bat_charging"):
        tag = "充电中"
    elif st.get("bat_ac"):
        tag = "交流供电"
    else:
        tag = "放电中"
    put(d, (b["x"] + 8, b["y"] + b["h"] - 10), tag, _fs(b["h"], 0.15, 9, 13), "reg", DIM, anchor="ls")
    bar(d, b["x"] + 8, b["y"] + b["h"] * 0.64, b["w"] - 16, max(5, int(b["h"] * 0.10)), p, c)


def draw_uptime(d, b, st, ctx):
    card(d, b)
    cap(d, b, "运行时长")
    put(d, (b["x"] + b["w"] / 2, b["y"] + b["h"] * 0.66), txt(st.get("uptime")), _fs(b["h"], 0.30, 11, 26), "mono", TXT, anchor="mm")


def draw_volume(d, b, st, ctx):
    card(d, b)
    cap(d, b, "音量")
    v = st.get("vol_pct")
    c = ACCENT if v is not None else NA
    put(d, (b["x"] + 8, b["y"] + b["h"] * 0.55), num(v, 0), _fs(b["h"], 0.36, 13, 34), "bd", c, anchor="lm")
    put(d, (b["x"] + b["w"] - 8, b["y"] + b["h"] * 0.55), "%" if v is not None else "", _fs(b["h"], 0.18, 10, 16), "bd", c, anchor="rm")
    if v is not None:
        bar(d, b["x"] + 8, b["y"] + b["h"] * 0.74, b["w"] - 16, max(4, int(b["h"] * 0.09)), v, ACCENT)


def draw_host(d, b, st, ctx):
    card(d, b)
    cap(d, b, "主机")
    put(d, (b["x"] + 8, b["y"] + b["h"] * 0.68), txt(st.get("host"))[:28], _fs(b["h"], 0.30, 10, 22), "mono", TXT, anchor="lm")


def draw_ip(d, b, st, ctx):
    card(d, b)
    cap(d, b, "IP")
    put(d, (b["x"] + 8, b["y"] + b["h"] * 0.68), txt(st.get("ip"))[:24], _fs(b["h"], 0.30, 10, 22), "mono", TXT, anchor="lm")


def draw_top_proc(d, b, st, ctx):
    card(d, b)
    cap(d, b, "最占进程")
    name = st.get("top_proc")
    p = st.get("top_proc_pct")
    put(d, (b["x"] + 8, b["y"] + b["h"] * 0.55), txt(name)[:20], _fs(b["h"], 0.28, 11, 24), "mono", TXT, anchor="lm")
    put(d, (b["x"] + b["w"] - 8, b["y"] + b["h"] * 0.85), ("%s%%" % num(p, 1)) if p is not None else "n/a",
        _fs(b["h"], 0.16, 9, 14), "mono", pick(p), anchor="rs")


def draw_history_cpu(d, b, st, ctx):
    card(d, b)
    cap(d, b, "CPU 历史")
    hist = (ctx or {}).get("history") or []
    x0, y0 = b["x"] + 8, b["y"] + b["h"] * 0.30
    ww, hh = b["w"] - 16, b["h"] * 0.60
    if ww < 8 or hh < 8:
        return
    d.line([x0, y0 + hh, x0 + ww, y0 + hh], fill=LINE)
    if len(hist) < 2:
        put(d, (b["x"] + b["w"] / 2, y0 + hh / 2), "n/a（需要 --state-dir）", _fs(b["h"], 0.16, 9, 13), "reg", NA, anchor="mm")
        return
    vals = [h.get("cpu") for h in hist]
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        put(d, (b["x"] + b["w"] / 2, y0 + hh / 2), "n/a", _fs(b["h"], 0.20, 10, 16), "mono", NA, anchor="mm")
        return
    n = len(vals)
    step = ww / float(n - 1)
    pts = [(x0 + i * step, y0 + hh - (max(0.0, min(100.0, float(v))) / 100.0) * hh) for i, v in enumerate(vals)]
    d.line(pts, fill=ACCENT, width=2, joint="curve")
    cur = vals[-1]
    put(d, (b["x"] + b["w"] - 8, b["y"] + 6), "%s%%" % num(cur, 0), _fs(b["h"], 0.17, 9, 15), "bd", pick(cur), anchor="ra")
    put(d, (b["x"] + 8, b["y"] + b["h"] - 10), "共 %d 点" % n, _fs(b["h"], 0.15, 9, 12), "reg", FAINT, anchor="ls")


DRAW_FNS = {
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
def render(st, widgets=None, preset="default", quality=72, limit=None, history=None):
    """返回 (jpeg_bytes, PIL.Image)。st 里缺失/null 的字段一律画 n/a，不崩。"""
    if not isinstance(st, dict):
        st = {}
    ids = resolve_widgets(widgets, preset)
    boxes, info = layout(ids)

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    # 顶部一条极细的强调线，避免整体过暗
    d.rectangle([0, 0, W, 2], fill=(24, 28, 38))
    d.text((W - 8, H - 4), "480x360", font=F(10, "mono"), fill=(46, 52, 66), anchor="rs")

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


def main(argv=None):
    ap = argparse.ArgumentParser(description="状态 JSON → 480x360 baseline JPEG")
    ap.add_argument("--in", dest="inp", default=None, help="状态 JSON 路径（- 表示 stdin）")
    ap.add_argument("--out", dest="out", default=None, help="输出 JPEG 路径")
    ap.add_argument("--widgets", default=None, help="逗号分隔的部件 id，优先于 --preset")
    ap.add_argument("--preset", default="default", choices=sorted(PRESETS))
    ap.add_argument("--quality", type=int, default=72)
    ap.add_argument("--limit", type=int, default=None, help="字节上限，超出则二分降质量")
    ap.add_argument("--preview", default=None, help="另存一张 PNG 预览")
    ap.add_argument("--state-dir", dest="state_dir", default=None, help="历史采样目录（供 history_cpu）")
    ap.add_argument("--list", action="store_true", help="打印部件注册表 JSON 后退出")
    ap.add_argument("--layout", action="store_true", help="打印布局盒子 JSON 后退出")
    a = ap.parse_args(argv)

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
                           a.quality, a.limit, hist)
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
