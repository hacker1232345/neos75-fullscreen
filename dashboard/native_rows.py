"""Turn sampled host state into the four ASCII text rows the native channel can carry.

The native overlay writes text into RAM and lets the keyboard's own 250 ms timer
redraw it, so this path has none of the media pipeline's throttling. The trade-off
is the wire format: the firmware stores 4 rows x 32 bytes of printable ASCII
(0x20..0x7e), so this module speaks only ASCII and never invents a value that the
sample does not contain - a missing sensor becomes "n/a".
"""
import datetime as dt
import json
import math
import re
from pathlib import Path

MAX_ROWS = 4
ROW_BYTES = 32          # older/portable firmware has 32 bytes per row
MAX_TEXT = ROW_BYTES - 1
PRINTABLE = re.compile(r'[^\x20-\x7e]+')

registry_path = Path(__file__).resolve().parent / 'widgets.json'

# Widgets the 4x32-byte ASCII channel can carry. A line chart cannot: it is drawn,
# not written as text. Keep this list next to `tokens` so the two never drift.
TEXT_WIDGETS = ('clock_big', 'date', 'cpu', 'mem', 'gpu', 'temp', 'top_proc', 'disk',
                'net', 'ip', 'host', 'battery', 'volume', 'weather', 'codex', 'uptime',
                'clock_utc', 'day_progress', 'focus_timer', 'mem_available')
GRAPH_WIDGETS = ('history_cpu',)


def ascii_text(value, limit=MAX_TEXT):
    """Collapse anything non-printable out, so a Chinese process name still fits."""
    text = PRINTABLE.sub(' ', str(value))
    text = ' '.join(text.split())
    return text[:limit].rstrip()


def number(value, digits=0, unit=''):
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return f'{value:.{digits}f}{unit}'


def tokens(state, widget_id):
    """One short ASCII phrase per widget, or None when the value cannot be text.

    Returning a list lets a widget contribute two phrases; both are packed greedily.
    """
    w = state.get('weather') or {}
    c = state.get('codex') or {}
    if widget_id == 'clock_utc':
        return [f'UTC {state.get("clock_utc") or "n/a"}']
    if widget_id == 'day_progress':
        try:
            now = dt.time.fromisoformat(state.get('clock'))
            return [f'DAY {(now.hour * 60 + now.minute) * 100 // 1440}%']
        except (TypeError, ValueError):
            return ['DAY n/a']
    if widget_id == 'focus_timer':
        remaining = state.get('focus_remaining')
        if number(remaining) is None or remaining < 0:
            return ['FOCUS idle']
        minutes, seconds = divmod(int(remaining), 60)
        return [f'FOCUS {minutes:02d}:{seconds:02d}']
    if widget_id == 'mem_available':
        return [f'RAM FREE {number(state.get("mem_available_gb"), 1, "G") or "n/a"}']
    if widget_id == 'clock_big':
        return [state.get('clock')]
    if widget_id == 'date':
        return [state.get('date')]
    if widget_id == 'cpu':
        load = number(state.get('cpu_load'), 0, '%')
        freq = number(state.get('cpu_freq_ghz'), 1, 'G')
        return [f'CPU {load or "n/a"}' + (f' {freq}' if freq else '')]
    if widget_id == 'mem':
        pct = number(state.get('mem_pct'), 0, '%')
        used = number(state.get('mem_used_gb'), 1, 'G')
        return [f'RAM {pct or "n/a"}' + (f' {used}' if used else '')]
    if widget_id == 'gpu':
        return [f'GPU {number(state.get("gpu_pct"), 0, "%") or "n/a"}']
    if widget_id == 'temp':
        return [f'TEMP {number(state.get("temp_c"), 0, "C") or "n/a"}']
    if widget_id == 'top_proc':
        name = ascii_text(state.get('top_proc') or '', 14)
        pct = number(state.get('top_proc_pct'), 0, '%')
        return [f'TOP {name} {pct}' if name and pct else None]
    if widget_id == 'disk':
        free = number(state.get('disk_free_gb'), 0, 'G')
        return [f'DISK {free} FREE' if free else None]
    if widget_id == 'net':
        down, up = number(state.get('net_down_mbps'), 2), number(state.get('net_up_mbps'), 2)
        return [f'NET {down or "0.00"}/{up or "0.00"} Mbps' if down or up else None]
    if widget_id == 'ip':
        return [f'IP {ascii_text(state["ip"], 20)}' if state.get('ip') else None]
    if widget_id == 'host':
        return [f'HOST {ascii_text(state["host"], 20)}' if state.get('host') else None]
    if widget_id == 'battery':
        pct = number(state.get('bat_pct'), 0, '%')
        if not pct:
            return None
        return [f'BAT {pct} ' + ('AC' if state.get('bat_ac') else 'BATT')]
    if widget_id == 'volume':
        return [f'VOL {number(state.get("vol_pct"), 0, "%") or "n/a"}']
    if widget_id == 'uptime':
        return [f'UP {ascii_text(state.get("uptime"), 24)}' if state.get('uptime') else None]
    if widget_id == 'weather':
        if w.get('status') != 'ok' or number(w.get('temperature'), 0) is None:
            return ['WX n/a']
        humid = number(w.get('humidity'), 0, '%')
        return [f'WX {number(w["temperature"], 0, "C")}' + (f' {humid}' if humid else '')]
    if widget_id == 'codex':
        age = number(c.get('age'), 0, 's')
        return [f'CODEX {ascii_text(c.get("state") or "unknown", 12)}' + (f' {age}' if age else '')]
    # A line chart and any unknown id cannot be expressed as text rows.
    return None


def rows_from_state(state, widgets, registry=None):
    """Pack the chosen widgets into at most four 31-character ASCII rows.

    Returns (rows, omitted) where `omitted` names widgets the text channel cannot show
    and any widgets that did not fit, so the caller can say so instead of silently
    dropping them.
    """
    state = state if isinstance(state, dict) else {}
    widgets = [w for w in (widgets or []) if isinstance(w, str)]
    phrases, omitted, seen = [], [], set()
    for widget_id in widgets:
        if widget_id in seen:
            continue
        seen.add(widget_id)
        produced = tokens(state, widget_id)
        if not produced:
            omitted.append(widget_id)
            continue
        phrases.extend(p for p in (ascii_text(x, MAX_TEXT) for x in produced if x) if p)
    rows, overflow = [], 0
    for phrase in phrases:
        if len(phrase) > MAX_TEXT:
            phrase = phrase[:MAX_TEXT]
        if rows and len(rows[-1]) + 2 + len(phrase) <= MAX_TEXT:
            rows[-1] = rows[-1] + '  ' + phrase
            continue
        if len(rows) < MAX_ROWS:
            rows.append(phrase)
            continue
        overflow += 1
    rows += [''] * (MAX_ROWS - len(rows))
    if overflow and len(rows) == MAX_ROWS:
        marker = f' +{overflow}'
        rows[-1] = rows[-1][:MAX_TEXT - len(marker)] + marker
    return rows, omitted


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='把采样状态转成原生组件四行 ASCII 文本')
    parser.add_argument('--in', dest='source', default='-', help='状态 JSON 文件，- 读 stdin')
    parser.add_argument('--widgets', default='', help='逗号分隔的部件 id，默认取注册表 default')
    parser.add_argument('--list', action='store_true', help='只打印文本通道支持的部件')
    args = parser.parse_args(argv)
    registry = json.loads(registry_path.read_text(encoding='utf-8'))
    if isinstance(registry, dict):
        registry = registry['widgets']
    if args.list:
        supported = [w['id'] for w in registry if w['id'] in TEXT_WIDGETS]
        unsupported = [w['id'] for w in registry if w['id'] not in TEXT_WIDGETS]
        print(json.dumps({'supported': supported, 'unsupported': unsupported,
                          'max_rows': MAX_ROWS, 'max_text': MAX_TEXT}, ensure_ascii=False))
        return 0
    import sys
    if args.source == '-':
        state = json.loads(sys.stdin.read() or '{}')
    else:
        state = json.loads(Path(args.source).read_text(encoding='utf-8'))
    chosen = [x.strip() for x in args.widgets.split(',') if x.strip()] or [
        w['id'] for w in registry if w.get('default')]
    rows, omitted = rows_from_state(state, chosen, registry)
    print(json.dumps({'rows': rows, 'omitted': omitted}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
