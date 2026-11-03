"""Optional sources: public city weather and local Codex event metadata only."""
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import urllib.parse
import urllib.request


def json_get(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'Neos75/1.4 weather'})
    with urllib.request.urlopen(request, timeout=8) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError('天气响应过大')
    return json.loads(raw)


def location(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('请选择搜索结果中的城市。')
    label = value.get('label')
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
        raise ValueError('城市名称无效。')
    lat, lon = value.get('latitude'), value.get('longitude')
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (lat, lon)):
        raise ValueError('城市坐标无效。')
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError('城市坐标超出范围。')
    return dict(label=' '.join(label.split()), latitude=lat, longitude=lon)


def search_cities(query, fetch=json_get):
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 80:
        raise ValueError('请输入 2 至 80 个字的城市名称。')
    url = 'https://geocoding-api.open-meteo.com/v1/search?' + urllib.parse.urlencode(
        dict(name=query.strip(), count=8, language='zh', format='json'))
    rows = fetch(url).get('results', [])
    return [location(dict(label=' / '.join(dict.fromkeys(str(r[k]) for k in ('name', 'admin1', 'country') if r.get(k))),
                          latitude=r['latitude'], longitude=r['longitude'])) for r in rows[:8]]


def weather_label(code):
    groups = [({0}, '晴'), ({1, 2}, '少云'), ({3}, '多云'), ({45, 48}, '雾'),
              ({51, 53, 55, 56, 57}, '毛毛雨'), ({61, 63, 65, 66, 67, 80, 81, 82}, '雨'),
              ({71, 73, 75, 77, 85, 86}, '雪'), ({95, 96, 99}, '雷雨')]
    return next((label for codes, label in groups if code in codes), '天气未知')


class Weather:
    def __init__(self, fetch=json_get, clock=time.time):
        self.fetch, self.clock = fetch, clock
        self.lock = threading.Lock()
        self.cache = {}
        self.next_try = {}
        self.pending = set()
        self.errors = set()

    def read(self, city):
        if not city:
            return dict(status='unconfigured', label='请选择城市')
        key = (city['latitude'], city['longitude'])
        now = self.clock()
        with self.lock:
            if now >= self.next_try.get(key, 0) and key not in self.pending:
                self.pending.add(key)
                threading.Thread(target=self._update, args=(key,), daemon=True).start()
            value = dict(self.cache.get(key, {}))
            value.update(city=city['label'])
            if not value.get('updated_at'):
                value.update(status='unavailable' if key in self.errors else 'loading',
                             label='天气暂不可用' if key in self.errors else '正在获取天气')
            else:
                value['status'] = 'stale' if key in self.errors or now - value['updated_at'] > 1800 else 'ok'
                # Old weather must not masquerade as current after an outage.
                if now - value['updated_at'] > 3600:
                    value.update(status='unavailable', temperature=None, label='天气数据已过期')
            return value

    def _update(self, key):
        try:
            url = 'https://api.open-meteo.com/v1/forecast?' + urllib.parse.urlencode(dict(
                latitude=key[0], longitude=key[1], current='temperature_2m,relative_humidity_2m,weather_code',
                timezone='auto', forecast_days=1))
            data = self.fetch(url)['current']
            temperature = data['temperature_2m']
            if isinstance(temperature, bool) or not isinstance(temperature, (float, int)) or not math.isfinite(temperature):
                raise ValueError('无效温度')
            value = dict(temperature=temperature, humidity=data.get('relative_humidity_2m'),
                         label=weather_label(data.get('weather_code')), observed_at=str(data.get('time', ''))[:19],
                         updated_at=self.clock())
            with self.lock:
                self.cache[key] = value
                self.errors.discard(key)
                self.next_try[key] = self.clock() + 600
        except (OSError, ValueError, KeyError, TypeError):
            with self.lock:
                self.errors.add(key)
                self.next_try[key] = self.clock() + 60
        finally:
            with self.lock:
                self.pending.discard(key)


THREAD_ID = re.compile(r'(?:[a-zA-Z0-9_-]{8,80})\Z')
STATES = {'running': '运行中', 'waiting': '等待操作', 'completed': '本轮已完成',
          'failed': '执行出错', 'interrupted': '已中断', 'activity': '近期有活动',
          'unknown': '暂无新事件', 'unavailable': '未发现本地任务'}


def event_state(event):
    """Interpret terminal state only from an explicit event, never from silence."""
    method = event.get('method')
    params = event.get('params') or {}
    if method == 'thread/status/changed':
        status = params.get('status') or {}
        if status.get('type') == 'active':
            flags = status.get('activeFlags', [])
            return 'waiting' if any(f in flags for f in ('waitingOnApproval', 'waitingOnUserInput')) else 'running'
        return 'unknown'
    if method in ('turn/started', 'turn/completed'):
        status = (params.get('turn') or {}).get('status')
        return {'completed': 'completed', 'failed': 'failed', 'interrupted': 'interrupted'}.get(status, 'running' if method == 'turn/started' else 'unknown')
    payload = event.get('payload') or {}
    if event.get('type') == 'event_msg':
        return {'task_started': 'running', 'task_complete': 'completed', 'turn_aborted': 'interrupted',
                'error': 'failed'}.get(payload.get('type'))
    return None


class Codex:
    """Best-effort read-only adapter; the rollout format is not a stable API."""
    def __init__(self, home=None, clock=time.time):
        self.home = Path(home or os.environ.get('CODEX_HOME', Path.home() / '.codex'))
        self.clock = clock
        self.selected = None
        self.last_scan = 0

    def read(self, thread=''):
        now = self.clock()
        try:
            # A client consuming the documented app-server stream can supply an
            # explicit status snapshot. No app-server or paid request is started.
            bridge = self.home / 'neos75-progress.json'
            if bridge.is_file() and bridge.stat().st_size < 65536:
                value = json.loads(bridge.read_text(encoding='utf-8'))
                state = value.get('state')
                age = now - float(value.get('updated_at', 0))
                if state in STATES and 0 <= age < 120 and (not thread or value.get('thread_id') == thread):
                    return dict(state=state, label=STATES[state], age=int(age), source='事件桥')
            # Scan only date directories and JSONL files, never auth/config/DBs.
            if now - self.last_scan > 5 or self.selected is None or self.selected[0] != thread:
                self.last_scan = now
                days = sorted((self.home / 'sessions').glob('*/*/*'), reverse=True)[:7]
                files = [p for day in days if day.is_dir() for p in day.glob('*.jsonl') if not thread or p.stem.endswith(thread)]
                self.selected = (thread, max(files, key=lambda p: p.stat().st_mtime) if files else None)
            path = self.selected[1]
            if path is None:
                return dict(state='unavailable', label=STATES['unavailable'], source='本地事件')
            stat = path.stat()
            # Bound cold reads even for image-heavy multi-hundred-MB sessions.
            with path.open('rb') as f:
                start = max(0, stat.st_size - 1048576)
                f.seek(start)
                if start:
                    f.readline(1048576)  # discard a partial first record
                lines = f.read(1048576).splitlines()
            state, stamp = None, None
            for line in reversed(lines):
                try:
                    event = json.loads(line)
                    state = event_state(event)
                    if state:
                        stamp = dt.datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')).timestamp()
                        break
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
            age = max(0, int(now - (stamp if stamp is not None else stat.st_mtime)))
            state = state or ('activity' if age < 120 else 'unknown')
            if state in ('running', 'waiting') and age >= 120:
                state = 'unknown'
            return dict(state=state, label=STATES[state], age=age, source='本地事件')
        except (OSError, ValueError, TypeError, AttributeError):
            return dict(state='unavailable', label='Codex 状态暂不可用', source='本地事件')


class Sources:
    def __init__(self):
        self.weather = Weather()
        self.codex = Codex()

    def read(self, widgets, settings):
        result = {}
        if 'weather' in widgets:
            result['weather'] = self.weather.read(settings.get('weather'))
        if 'codex' in widgets:
            result['codex'] = self.codex.read(settings.get('codex_thread', ''))
        return result
