import base64
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import tkinter as tk

SERVICE_FILE = os.path.expanduser('~/.local/state/opencode/service.json')
BASIC_USER = 'opencode'
LOOPBACK_HOSTS = frozenset(('localhost', '127.0.0.1', '::1'))
SPEED_MIN_SECONDS = 0.05
SPEED_MAX_SECONDS = 600.0
SCAN_LIMIT = 5


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))


class ServiceError(Exception):
    pass


def calc_speed(tokens, created, completed):
    if tokens is None or not isinstance(created, (int, float)) or not isinstance(completed, (int, float)):
        return None, None
    seconds = (completed - created) / 1000.0
    total = (tokens.get('output', 0) or 0) + (tokens.get('reasoning', 0) or 0)
    if total <= 0 or seconds < SPEED_MIN_SECONDS or seconds > SPEED_MAX_SECONDS:
        return None, None
    return total / seconds, seconds


def calc_hit(tokens):
    if not tokens:
        return None
    cache = tokens.get('cache') or {}
    read = cache.get('read', 0) or 0
    write = cache.get('write', 0) or 0
    if read == 0 and write == 0:
        return None
    total = (tokens.get('input', 0) or 0) + read + write
    return read / total * 100.0 if total > 0 else None


def calc_stats(message):
    info = message.get('time') or {}
    tokens = message.get('tokens')
    speed, seconds = calc_speed(tokens, info.get('created'), info.get('completed'))
    return {
        'tokens': tokens if tokens else None,
        'hit': calc_hit(tokens),
        'speed': speed,
        'seconds': seconds,
        'model': message.get('model'),
    }


def is_completed(message):
    info = message.get('time') or {}
    return bool(info.get('completed')) and bool(message.get('tokens'))


def find_completed(messages):
    checked = 0
    for message in messages:
        if message.get('type') != 'assistant' or not message.get('id'):
            continue
        if checked >= SCAN_LIMIT:
            return None
        checked += 1
        if is_completed(message):
            return message
    return None


def fmt_tok(value):
    value = int(value)
    if value >= 1000000:
        return '%gM' % round(value / 1000000, 1)
    if value >= 1000:
        return '%gK' % round(value / 1000, 1)
    return str(value)


def fmt_speed(value, seconds):
    if value is None:
        return '計測不可'
    if value >= 1000:
        out = '%.1fK tok/s' % (value / 1000)
    else:
        out = '%.1f tok/s' % value
    if seconds is not None:
        out += ' (%.1fs)' % seconds
    return out


def fmt_hit(value):
    return '未計測' if value is None else '%.1f%%' % value


def fmt_model(model):
    if not model:
        return '-'
    text = '%s / %s' % (model.get('providerID', ''), model.get('id', ''))
    variant = model.get('variant')
    return text + (' (%s)' % variant if variant else '')


class Service:
    def __init__(self):
        self.url = None
        self.auth = None
        self.mtime = 0.0

    def invalidate(self):
        self.url = None
        self.auth = None
        self.mtime = 0.0

    def reload(self):
        try:
            current = os.path.getmtime(SERVICE_FILE)
        except OSError:
            current = None
        if current == self.mtime and self.mtime:
            return self.url is not None
        if not current:
            self.invalidate()
            return False
        try:
            with open(SERVICE_FILE, encoding='utf-8') as handle:
                reg = json.load(handle)
            raw_url = reg.get('url')
            password = reg.get('password')
        except (OSError, ValueError):
            return False
        if not raw_url or not password:
            return False
        parsed = urllib.parse.urlparse(str(raw_url))
        if parsed.scheme != 'http' or parsed.hostname not in LOOPBACK_HOSTS:
            return False
        token = 'Basic ' + base64.b64encode(
            (BASIC_USER + ':' + password).encode('utf-8')).decode('ascii')
        self.url = str(raw_url).rstrip('/')
        self.auth = token
        self.mtime = current
        return True

    def get(self, path):
        if not self.reload() or not self.url:
            raise ServiceError('service not registered')
        request = urllib.request.Request(self.url + path, headers={
            'Authorization': self.auth,
            'Accept': 'application/json',
        })
        try:
            with OPENER.open(request, timeout=5) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as error:
            if error.code == 401:
                self.invalidate()
            raise ServiceError('HTTP %s' % error.code) from error
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            raise ServiceError(str(error)) from error

    def stream(self):
        if not self.reload() or not self.url:
            raise ServiceError('service not registered')
        request = urllib.request.Request(self.url + '/api/event', headers={
            'Authorization': self.auth,
            'Accept': 'text/event-stream',
        })
        return OPENER.open(request, timeout=30)

    def session_info(self, sid):
        return self.get('/api/session/' + sid).get('data') or {}

    def session_messages(self, sid):
        return self.get('/api/session/%s/message' % sid).get('data') or []

    def message_detail(self, sid, mid):
        return self.get('/api/session/%s/message/%s' % (sid, mid)).get('data') or {}

    def active_sessions(self):
        data = self.get('/api/session/active').get('data')
        return list(data) if isinstance(data, dict) else []

    def recent_sessions(self):
        return (self.get('/api/session').get('data') or [])[:20]


class App:
    def __init__(self):
        self.service = Service()
        self.q = queue.Queue()
        self.apiq = queue.Queue()
        self.running = True

        self.target_sid = None
        self.pinned_sid = None
        self.active_sids = frozenset()
        self.recent = []
        self.connected = False
        self.status = 'idle'
        self.last_speed = None
        self.last_seconds = None
        self.last_hit = None
        self.last_tokens = None
        self.current_model = None
        self.session_titles = {}

        self.root = tk.Tk()
        self.root.title('OpenCode 統計')
        self.root.geometry('330x178+80+80')
        self.root.attributes('-topmost', True)
        self.root.resizable(False, False)
        self.root.protocol('WM_DELETE_WINDOW', self.close)

        self.menu_pin = tk.StringVar(value='__auto__')
        self.topmost_var = tk.BooleanVar(value=True)

        background = '#141414'
        frame = tk.Frame(self.root, bg=background)
        frame.pack(fill='both', expand=True, padx=10, pady=8)

        def label(size, weight, fg, text):
            item = tk.Label(frame, text=text, bg=background, fg=fg,
                            font=('Microsoft YaHei UI', size, weight), anchor='w')
            item.pack(fill='x')
            return item

        self.status_label = label(9, 'bold', '#9a9a9a', '未接続')
        self.speed_label = label(17, 'bold', '#7ec8ff', '計測不可')
        self.hit_label = label(9, 'normal', '#e2e2e2', 'cache hit: 未計測')
        self.cache_label = label(9, 'normal', '#e2e2e2', 'read: -  write: -')
        self.model_label = label(8, 'normal', '#9a9a9a', 'model: -')
        self.session_label = label(8, 'normal', '#9a9a9a', 'session: -')

        self.root.bind('<Button-3>', self.menu)
        threading.Thread(target=self.event_loop, daemon=True).start()
        threading.Thread(target=self.api_worker, daemon=True).start()
        self.root.after(250, self.poll_queue)

    def menu(self, event):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label='統計を再取得',
                         command=lambda: self.apiq.put(('refresh-all', None, None)))
        menu.add_separator()
        menu.add_radiobutton(label='自動（実行中セッションを追従）',
                             variable=self.menu_pin, value='__auto__',
                             command=self.release_pin)
        for sid, title, running in self.recent:
            mark = ('[実行中] ' if running else '') + (title or sid)
            menu.add_radiobutton(label=mark, variable=self.menu_pin, value=sid,
                                 command=lambda s=sid: self.pin_session(s))
        menu.add_separator()
        menu.add_checkbutton(label='常に最前面', variable=self.topmost_var,
                             command=self.toggle_topmost)
        menu.add_command(label='閉じる', command=self.close)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def toggle_topmost(self):
        self.root.attributes('-topmost', self.topmost_var.get())

    def pin_session(self, sid):
        self.pinned_sid = sid
        self.switch_target(sid)

    def release_pin(self):
        self.pinned_sid = None

    def event_loop(self):
        while self.running:
            try:
                response = self.service.stream()
            except Exception:
                self.q.put(('connected', None, False))
                time.sleep(2)
                continue
            self.q.put(('connected', None, True))
            buffer = b''
            try:
                while self.running:
                    chunk = response.read1(8192)
                    if not chunk:
                        break
                    buffer += chunk
                    while True:
                        index = buffer.find(b'\n\n')
                        if index < 0:
                            break
                        frame, buffer = buffer[:index], buffer[index + 2:]
                        self.emit_frame(frame)
            except Exception:
                pass
            finally:
                try:
                    response.close()
                except Exception:
                    pass
            self.q.put(('connected', None, False))
            time.sleep(2)

    def emit_frame(self, frame):
        text = frame.decode('utf-8', 'replace').strip()
        if not text.startswith('data: '):
            return
        try:
            payload = json.loads(text[6:])
        except ValueError:
            return
        kind = payload.get('type') or ''
        data = payload.get('data')
        if not isinstance(data, dict):
            return
        sid = data.get('sessionID')
        if not sid:
            return
        if kind.startswith('session.execution.'):
            self.q.put(('execution', sid, kind.rsplit('.', 1)[-1]))
        elif kind == 'session.step.ended':
            mid = data.get('assistantMessageID')
            if mid:
                self.apiq.put(('step-detail', sid, mid))

    def api_worker(self):
        last_refresh = 0.0
        while self.running:
            try:
                kind, sid, mid = self.apiq.get(timeout=1.0)
                if kind == 'refresh-all':
                    last_refresh = time.time()
                    self.job_refresh_all()
                elif kind == 'target-sync':
                    self.job_target_sync(sid)
                elif kind == 'step-detail':
                    self.job_step_detail(sid, mid)
            except queue.Empty:
                now = time.time()
                if self.connected and now - last_refresh >= 15.0:
                    last_refresh = now
                    try:
                        self.job_refresh_all()
                    except Exception:
                        self.q.put(('connected', None, False))
                continue
            except Exception:
                self.q.put(('connected', None, False))

    def job_refresh_all(self):
        active = self.service.active_sessions()
        self.q.put(('active-list', frozenset(active), None))
        recent = []
        titles = {}
        for session in self.service.recent_sessions():
            sid = session.get('id')
            if not sid:
                continue
            title = session.get('title') or 'no title'
            recent.append((sid, title, sid in active))
            titles[sid] = title
        self.q.put(('session-list', recent, titles))

    def job_target_sync(self, sid):
        info = self.service.session_info(sid)
        title = info.get('title') or 'no title'
        stat = calc_stats(find_completed(self.service.session_messages(sid)) or {})
        self.q.put(('target-stats', sid, {
            'title': title,
            'stat': stat,
            'model': stat.get('model') or info.get('model'),
            'running': sid in self.service.active_sessions(),
        }))

    def job_step_detail(self, sid, mid):
        detail = self.service.message_detail(sid, mid)
        if is_completed(detail):
            self.q.put(('step-stats', sid, calc_stats(detail)))

    def poll_queue(self):
        if not self.running:
            return
        while True:
            try:
                kind, param, extra = self.q.get_nowait()
            except queue.Empty:
                break
            self.dispatch(kind, param, extra)
        self.render()
        if self.running:
            self.root.after(250, self.poll_queue)

    def dispatch(self, kind, param, extra):
        if kind == 'connected':
            changed = self.connected != bool(extra)
            self.connected = bool(extra)
            if not self.connected:
                self.status = 'idle'
            elif changed:
                self.reset_stats()
                self.apiq.put(('refresh-all', None, None))
        elif kind == 'active-list':
            self.active_sids = param or frozenset()
            if self.target_sid not in self.active_sids and self.status == 'running':
                self.status = 'idle'
        elif kind == 'session-list':
            self.recent = param or []
            self.session_titles = extra or {}
            self.apply_target()
        elif kind == 'target-stats':
            sid, data = param, extra
            if sid != self.target_sid:
                return
            title = data.get('title')
            if title and title != 'no title':
                self.session_titles[sid] = title
            stat = data.get('stat') or {}
            if stat.get('speed') is not None or stat.get('tokens'):
                self.apply_stats(stat)
            self.current_model = data.get('model') or self.current_model
            if data.get('running'):
                self.status = 'running'
        elif kind == 'step-stats':
            sid, data = param, extra
            if sid != self.target_sid:
                return
            self.apply_stats(data)
        elif kind == 'execution':
            self.on_execution(param, extra)

    def on_execution(self, sid, sub):
        if sub == 'started':
            if self.pinned_sid and sid != self.pinned_sid:
                return
            self.switch_target(sid)
            self.status = 'running'
        elif self.target_sid == sid and sub in ('succeeded', 'failed', 'interrupted'):
            self.status = 'idle'

    def switch_target(self, sid):
        if not sid:
            return
        if sid != self.target_sid:
            self.target_sid = sid
            self.reset_stats()
        self.apiq.put(('target-sync', sid, None))

    def apply_target(self):
        if self.pinned_sid:
            sid = self.pinned_sid
        elif self.target_sid and self.target_sid in self.active_sids:
            sid = self.target_sid
        elif self.active_sids:
            candidates = [sid for sid, title, running in self.recent
                          if sid in self.active_sids]
            sid = candidates[0] if candidates else self.target_sid
        elif self.target_sid:
            sid = self.target_sid
        elif self.recent:
            sid = self.recent[0][0]
        else:
            return
        self.switch_target(sid)

    def reset_stats(self):
        self.last_tokens = None
        self.last_hit = None
        self.last_speed = None
        self.last_seconds = None
        self.current_model = None

    def apply_stats(self, data):
        self.last_tokens = data.get('tokens')
        self.last_hit = data.get('hit')
        self.last_speed = data.get('speed')
        self.last_seconds = data.get('seconds')
        self.current_model = data.get('model') or self.current_model

    def render(self):
        if not self.connected:
            text, color = '未接続', '#ff8e8e'
        elif self.status == 'running':
            text, color = '生成中', '#5ad469'
        else:
            text, color = '待機', '#9a9a9a'
        self.status_label.config(text=text, fg=color)
        self.speed_label.config(text=fmt_speed(self.last_speed, self.last_seconds))
        self.hit_label.config(text='cache hit: ' + fmt_hit(self.last_hit))
        if self.last_tokens:
            cache = self.last_tokens.get('cache') or {}
            self.cache_label.config(text='read: %s  write: %s' % (
                fmt_tok(cache.get('read', 0) or 0),
                fmt_tok(cache.get('write', 0) or 0)))
        else:
            self.cache_label.config(text='read: -  write: -')
        self.model_label.config(text='model: ' + fmt_model(self.current_model))
        sid = self.target_sid
        title = self.session_titles.get(sid) if sid else None
        label = title if title else (sid or '-')
        pin = '[固定] ' if self.pinned_sid == sid else ''
        self.session_label.config(text='session: ' + pin + label)

    def close(self):
        self.running = False
        self.root.destroy()


def main():
    App().root.mainloop()


if __name__ == '__main__':
    main()
