import base64
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
import tkinter as tk

SERVICE_FILE = os.path.expanduser('~/.local/state/opencode/service.json')
BASIC_USER = 'opencode'
SPEED_MIN_SECONDS = 0.05
SPEED_MAX_SECONDS = 600.0


class ServiceError(Exception):
    pass


def calc_speed(tokens, streamed, completed):
    if not isinstance(streamed, (int, float)) or not isinstance(completed, (int, float)):
        return None
    return calc_speed_seconds(tokens, (completed - streamed) / 1000.0)


def calc_speed_seconds(tokens, seconds):
    if tokens is None or seconds <= 0:
        return None
    total = (tokens.get('output', 0) or 0) + (tokens.get('reasoning', 0) or 0)
    if total <= 0:
        return None
    if seconds < SPEED_MIN_SECONDS or seconds > SPEED_MAX_SECONDS:
        return None
    return total / seconds


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


def fmt_tok(value):
    value = int(value)
    if value >= 1000000:
        return '%gM' % round(value / 1000000, 1)
    if value >= 1000:
        return '%gK' % round(value / 1000, 1)
    return str(value)


def fmt_speed(value):
    if value is None:
        return '計測不可'
    if value >= 1000:
        return '%.1fK tok/s' % (value / 1000)
    return '%.1f tok/s' % value


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

    def reload(self):
        try:
            current = os.path.getmtime(SERVICE_FILE)
        except OSError:
            current = None
        if current == self.mtime and self.mtime:
            return self.url is not None
        if not current:
            self.url = None
            self.auth = None
            self.mtime = 0.0
            return False
        try:
            with open(SERVICE_FILE, encoding='utf-8') as handle:
                reg = json.load(handle)
            url = reg.get('url')
            password = reg.get('password')
        except (OSError, ValueError):
            return False
        if not url or not password:
            return False
        token = 'Basic ' + base64.b64encode(
            (BASIC_USER + ':' + password).encode('utf-8')).decode('ascii')
        self.url = str(url).rstrip('/')
        self.auth = token
        self.mtime = current
        return True

    def get(self, path):
        if not self.reload():
            raise ServiceError('service not registered')
        request = urllib.request.Request(self.url + path, headers={
            'Authorization': self.auth,
            'Accept': 'application/json',
        })
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as error:
            if error.code == 401:
                self.url = None
                self.auth = None
            raise ServiceError('HTTP %s' % error.code) from error
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            raise ServiceError(str(error)) from error

    def stream(self):
        if not self.reload():
            raise ServiceError('service not registered')
        request = urllib.request.Request(self.url + '/api/event', headers={
            'Authorization': self.auth,
            'Accept': 'text/event-stream',
        })
        return urllib.request.urlopen(request, timeout=30)

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
        return self.get('/api/session?limit=20&order=desc').get('data') or []


class App:
    def __init__(self):
        self.service = Service()
        self.q = queue.Queue()
        self.running = True

        self.target_sid = None
        self.pinned_sid = None
        self.active_sids = frozenset()
        self.recent = []
        self.connected = False
        self.status = 'idle'
        self.last_speed = None
        self.last_hit = None
        self.last_tokens = None
        self.current_model = None
        self.session_titles = {}
        self.monitored_at = 0.0
        self.refreshed_at = 0.0

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

        self.root.bind('<Button-3>', lambda event: self.menu(event))
        threading.Thread(target=self.event_loop, daemon=True).start()
        self.root.after(250, self.poll_queue)

    def menu(self, event=None):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label='統計を再取得',
                         command=lambda: self.q.put(('refresh-stats', None, None)))
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
        if event is not None:
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

    def toggle_topmost(self):
        self.root.attributes('-topmost', self.topmost_var.get())

    def pin_session(self, sid):
        self.pinned_sid = sid
        self.q.put(('session-change', sid, None))

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
        elif kind == 'session.step.started':
            if data.get('assistantMessageID'):
                self.q.put(('step-started', sid, data['assistantMessageID']))
        elif kind in ('session.step.ended', 'session.step.failed'):
            if data.get('assistantMessageID'):
                self.q.put(('step-ended', sid, data['assistantMessageID']))

    def dispatch(self, kind, param, extra):
        if kind == 'connected':
            self.on_connection(bool(extra))
        elif kind == 'execution':
            self.on_execution(param, extra)
        elif kind == 'step-started':
            self.on_step_started(param, extra)
        elif kind == 'step-ended':
            self.on_step_ended(param, extra)
        elif kind == 'refresh-stats':
            self.refresh_active()
            self.refresh_target()
            self.sync_last_message()
        elif kind == 'session-change':
            self.target_sid = param
            self.sync_last_message()

    def poll_queue(self):
        if not self.running:
            return
        while True:
            try:
                kind, param, extra = self.q.get_nowait()
            except queue.Empty:
                break
            try:
                self.dispatch(kind, param, extra)
            except ServiceError:
                self.connected = False
        now = time.time()
        if now - self.monitored_at >= 5.0:
            self.monitored_at = now
            self.service.reload()
            if now - self.refreshed_at >= 15.0:
                self.refreshed_at = now
                self.refresh_active()
                self.refresh_target()
        self.render()
        if self.running:
            self.root.after(250, self.poll_queue)

    def on_connection(self, value):
        changed = self.connected != value
        self.connected = value
        if not value:
            self.status = 'idle'
        elif changed:
            self.last_speed = None
            self.last_hit = None
            self.last_tokens = None
            self.refresh_active()
            self.refresh_target()
            self.sync_last_message()

    def on_execution(self, sid, kind):
        if self.pinned_sid and sid != self.pinned_sid:
            return
        if kind == 'started':
            if sid != self.target_sid:
                self.target_sid = sid
                self.refresh_active()
                self.sync_last_message()
                return
            self.status = 'running'
        elif self.target_sid == sid and kind in ('succeeded', 'failed', 'interrupted'):
            self.status = 'idle'
        self.render()

    def on_step_started(self, sid, mid):
        if self.pinned_sid and sid != self.pinned_sid:
            return
        if sid != self.target_sid:
            self.target_sid = sid
            self.refresh_active()
            self.sync_last_message()
            return
        self.status = 'running'
        self.render()

    def on_step_ended(self, sid, mid):
        if sid != self.target_sid:
            return
        try:
            message = self.service.message_detail(sid, mid)
        except ServiceError:
            return
        self.apply_message(message)
        self.status = 'idle'
        self.render()

    def refresh_active(self):
        try:
            self.active_sids = frozenset(self.service.active_sessions())
        except ServiceError:
            self.active_sids = frozenset()

    def refresh_target(self):
        try:
            sessions = self.service.recent_sessions()
        except ServiceError:
            sessions = []
        self.recent = []
        self.session_titles = {}
        for session in sessions:
            sid = session.get('id')
            if not sid:
                continue
            title = session.get('title') or 'no title'
            self.recent.append((sid, title, sid in self.active_sids))
            self.session_titles[sid] = title
        if self.pinned_sid:
            return
        if self.active_sids:
            if self.target_sid not in self.active_sids:
                for sid, title, running in self.recent:
                    if sid in self.active_sids:
                        self.target_sid = sid
                        self.sync_last_message()
                        break
        elif self.target_sid is None and self.recent:
            self.target_sid = self.recent[0][0]
            self.sync_last_message()

    def sync_last_message(self):
        if not self.target_sid:
            return
        try:
            info = self.service.session_info(self.target_sid)
            self.current_model = info.get('model') or self.current_model
            messages = self.service.session_messages(self.target_sid)
        except ServiceError:
            return
        self.last_tokens = None
        self.last_hit = None
        self.last_speed = None
        checked = 0
        for message in messages:
            if message.get('type') != 'assistant' or not message.get('id'):
                continue
            if checked >= 5:
                break
            checked += 1
            try:
                detail = self.service.message_detail(self.target_sid, message['id'])
            except ServiceError:
                continue
            if detail.get('tokens'):
                self.apply_message(detail)
                break
        self.status = 'running' if self.target_sid in self.active_sids else 'idle'
        self.render()

    def apply_message(self, message):
        info = message.get('time') or {}
        tokens = message.get('tokens')
        self.last_tokens = tokens if tokens else None
        self.last_hit = calc_hit(tokens)
        self.last_speed = calc_speed(tokens, info.get('streamed'), info.get('completed'))
        self.current_model = message.get('model') or self.current_model
        self.render()

    def render(self):
        if not self.connected:
            text, color = '未接続', '#ff8e8e'
        elif self.status == 'running':
            text, color = '生成中', '#5ad469'
        else:
            text, color = '待機', '#9a9a9a'
        self.status_label.config(text=text, fg=color)
        self.speed_label.config(text=fmt_speed(self.last_speed))
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
