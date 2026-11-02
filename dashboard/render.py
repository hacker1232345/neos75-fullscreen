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


def _frame(d, b, label=None, fill=CARD):
    """画卡片 + 左上角标签，返回内容区 (cx, cy, cw, ch)。

    cy 从标签下沿之后才开始，数值一律限制在内容区内，
    这样即使布局把行高压到 26px，标签与数值也不会重叠。
    """
    card(d, b, fill)
    lab = _fs(b["h"], 0.17, 8, 15)
    if label:
        put(d, (b["x"] + 7, b["y"] + 5), label, lab, "bd", DIM)
    cx = b["x"] + 7
    cy = b["y"] + lab + 7
    cw = max(4, b["w"] - 14)
    ch = max(4, b["y"] + b["h"] - 5 - cy)
    return cx, cy, cw, ch


def _metric(d, b, label, value, unit, color, sub=None, pct=None, fill=CARD):
    """标准指标卡：标签 + 大数值 + 单位 + 可选副行 + 可选进度条。

    纵向预算全部按内容区高度 ch 分配，因此任意格子高度都不会重叠或越出卡片。
    数值优先：装不下时先缩小字号，仍装不下就整段去掉单位（百分比落到副行）。
    """
    cx, cy, cw, ch = _frame(d, b, label, fill)
    bar_h = max(4, int(ch * 0.18)) if (pct is not None and ch >= 26) else 0
    sub_h = max(9, int(ch * 0.22)) if (sub and ch >= 22) else 0
    inner = ch - bar_h - sub_h - (3 if (bar_h or sub_h) else 0)
    vs = max(9, min(40, int(max(9, inner) * 0.94)))
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
    vtxt, vs = fitw(value, room, vs, "bd", 9)
    ymid = cy + max(9, inner) * 0.5
    put(d, (cx, ymid), vtxt, vs, "bd", color, anchor="lm")
    if unit:
        put(d, (cx + cw, ymid), unit, us, "bd", color, anchor="rm")
    y = cy + max(9, inner)
    if sub_h:
        stxt, ssz = fitw(sub, cw, sub_h, "mono", 8)
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
    cx, cy, cw, ch = _frame(d, b, "时间", CARD_HI)
    vs = max(14, min(54, int(ch * 0.80)))
    vtxt, vs = fitw(txt(st.get("clock"), "--:--:--"), cw, vs, "bd", 14)
    put(d, (cx + cw / 2, cy + ch / 2), vtxt, vs, "bd", ACCENT, anchor="mm")


def draw_date(d, b, st, ctx):
    cx, cy, cw, ch = _frame(d, b, "日期")
    vs = max(10, min(30, int(ch * 0.86)))
    vtxt, vs = fitw(txt(st.get("date")), cw, vs, "mono", 10)
    put(d, (cx + cw / 2, cy + ch / 2), vtxt, vs, "mono", TXT, anchor="mm")


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
        cx, cy, cw, ch = _frame(d, b, "电池")
        vs = max(12, min(34, int(ch * 0.80)))
        put(d, (cx + cw / 2, cy + ch / 2), "AC", vs, "bd", NA, anchor="mm")
        return
    c = GOOD if p > 30 else (WARN if p > 15 else BAD)
    tag = "充电中" if st.get("bat_charging") else ("交流供电" if st.get("bat_ac") else "放电中")
    _metric(d, b, "电池", num(p, 0), "%", c, sub=tag, pct=p)


def draw_uptime(d, b, st, ctx):
    cx, cy, cw, ch = _frame(d, b, "运行时长")
    vs = max(10, min(26, int(ch * 0.84)))
    vtxt, vs = fitw(txt(st.get("uptime")), cw, vs, "mono", 10)
    put(d, (cx + cw / 2, cy + ch / 2), vtxt, vs, "mono", TXT, anchor="mm")


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
        put(d, (cx + cw / 2, y0 + hh / 2), "n/a（需要 --state-dir）",
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
