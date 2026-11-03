"""Experimental page-6 text telemetry. Preview is offline from USB; send requires --yes.

Uses the desktop's existing read-only sensor/weather/Codex adapters, not image
uploads. Does not install firmware, change desktop settings or add Codex hooks.
"""
import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import time

PROJECT = Path(__file__).resolve().parents[2]


def number(value, digits=0):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or abs(value) > 9999:
        return 'N/A'
    return f'{value:.{digits}f}'


def metric(value, unit, digits=0):
    text = number(value, digits)
    return text if text == 'N/A' else text+unit


def rows(sample, extra):
    weather, codex = extra.get('weather', {}), extra.get('codex', {})
    weather_state = weather.get('status', 'unavailable')
    if weather_state in ('ok', 'stale'):
        label = {'晴':'CLEAR', '少云':'PARTLY CLOUDY', '多云':'CLOUDY', '雾':'FOG',
                 '毛毛雨':'DRIZZLE', '雨':'RAIN', '雪':'SNOW', '雷雨':'STORM'}.get(weather.get('label'), 'UNKNOWN')
        wx = f"WX {metric(weather.get('temperature'), 'C', 1)} {label}" + (' STALE' if weather_state == 'stale' else '')
    else:
        wx = 'WX ' + {'loading':'LOADING', 'unconfigured':'SET CITY ON PC'}.get(weather_state, 'N/A')
    state = codex.get('state', 'unknown')
    if state not in ('running','waiting','completed','failed','interrupted','activity','unknown','unavailable'):
        state = 'unknown'
    age = codex.get('age')
    age = f' {number(age)}s' if age is not None else ''
    result = [f"CPU {metric(sample.get('cpu_load'), '%')} TEMP {metric(sample.get('temp_c'), 'C')} RAM {metric(sample.get('mem_pct'), '%')}",
              wx, 'CODEX ' + state.upper() + age,
              f"BAT {metric(sample.get('bat_pct'), '%')} Updated {sample.get('clock', '--:--:--')}"]
    if any(not text.isascii() or len(text) > 47 or any(ord(c) < 32 or ord(c) > 126 for c in text) for text in result):
        raise ValueError('组件生成了不支持的字符或过长文字，未发送。')
    return result


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    modes = p.add_mutually_exclusive_group(required=True)
    modes.add_argument('--preview', action='store_true', help='仅输出一组文字，不打开USB')
    modes.add_argument('--send', action='store_true', help='向已安装匹配实验固件的设备发送')
    p.add_argument('--yes', action='store_true')
    p.add_argument('--settings', type=Path, help='只读配置；默认读取桌面程序的 settings.json')
    p.add_argument('--interval', type=float, default=5)
    p.add_argument('--count', type=int, default=0, help='发送次数，0表示持续，Ctrl+C结束')
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.send and not args.yes:
        p.error('--send 需要 --yes；应先运行 --preview。未打开USB。')
    if not math.isfinite(args.interval) or not 3 <= args.interval <= 10 or args.count < 0:
        p.error('interval须为3至10秒，count不能为负。')
    sys.path.insert(0, str(PROJECT / 'desktop'))
    from glass import Sampler, user_directory
    from data_sources import Sources, location, THREAD_ID
    settings_path = args.settings or user_directory() / 'settings.json'
    settings = {}
    if settings_path.exists():
        if settings_path.stat().st_size > 65536:
            raise ValueError('配置文件过大')
        raw = json.loads(settings_path.read_text(encoding='utf-8'))
        settings['weather'] = location(raw.get('weather'))
        thread = raw.get('codex_thread', '')
        if not isinstance(thread, str) or (thread and not THREAD_ID.fullmatch(thread)):
            raise ValueError('Codex任务ID无效')
        settings['codex_thread'] = thread
    sampler, sources = Sampler(), Sources()
    try:
        with tempfile.TemporaryDirectory(prefix='neos-native-') as temp:
            path = Path(temp) / 'rows.json'
            sent = 0
            while True:
                started = time.monotonic()
                value = rows(sampler.read(), sources.read(['weather', 'codex'], settings))
                print(json.dumps({'rows': value, 'usb': args.send}, ensure_ascii=False), flush=True)
                if args.preview:
                    return
                path.write_text(json.dumps(value), encoding='utf-8')
                # No shell, no image protocol, no retry, no firmware write path.
                subprocess.run(['node', str(Path(__file__).with_name('send.js')), '--rows', str(path), '--yes'],
                               cwd=PROJECT, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                sent += 1
                if args.count and sent >= args.count:
                    return
                time.sleep(max(0, args.interval-(time.monotonic()-started)))
    finally:
        sampler.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('已停止推流。')
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
