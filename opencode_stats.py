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
AVG_SAMPLES = 10


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))


class ServiceError(Exception):
    pass


def calc_speed(tokens, created, streamed, completed):
    """モデル応答区間 created→streamed を優先して計測する。

    streamed を持たない応答だけ completed にフォールバックし、方式を
    'measured' / 'fallback' で返す。二つの方式の数値を同じ平均に混ぜない。
    """
    if tokens is None or not isinstance(created, (int, float)):
        return None, None, None
    total = (tokens.get('output', 0) or 0) + (tokens.get('reasoning', 0) or 0)
    if total <= 0:
        return None, None, None
    candidates = []
    if isinstance(streamed, (int, float)) and streamed >= created:
        candidates.append((streamed, 'measured'))
    if isinstance(completed, (int, float)) and completed >= created:
        candidates.append((completed, 'fallback'))
    for end, method in candidates:
        seconds = (end - created) / 1000.0
        if SPEED_MIN_SECONDS <= seconds <= SPEED_MAX_SECONDS:
            return total / seconds, seconds, method
    return None, None, None


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
    """表示のため 1 メッセージの実効速度情報を取り出す。"""
    info = message.get('time') or {}
    speed, seconds, method = calc_speed(message.get('tokens'), info.get('created'),
                                        info.get('streamed'), info.get('completed'))
    created = info.get('created')
    return {
        'key': (created if isinstance(created, (int, float)) else 0, message.get('id') or ''),
        'speed': speed,
        'seconds': seconds,
        'method': method,
    }


def is_completed(message):
    info = message.get('time') or {}
    return bool(info.get('completed')) and bool(message.get('tokens'))


def model_key(model):
    if not isinstance(model, dict):
        return None
    return json.dumps(model, sort_keys=True)


def message_to_sample(message):
    """加重平均のサンプル。streamed基準(measured)の正常完了 step のみ。"""
    if not is_completed(message):
        return None
    info = message.get('time') or {}
    speed, seconds, method = calc_speed(message.get('tokens'), info.get('created'),
                                        info.get('streamed'), info.get('completed'))
    if method != 'measured':
        return None
    tokens = message.get('tokens')
    total = (tokens.get('output', 0) or 0) + (tokens.get('reasoning', 0) or 0)
    mid = message.get('id')
    if total <= 0 or not isinstance(mid, str) or not mid:
        return None
    return (info['created'], mid, total, seconds)


class Aggregation:
    """同一セッション・同一モデルの直近10サンプルの加重平均を管理する。

    samples は (created, mid, 出力トークン, 秒数) の昇順リスト。
    古い応答は (created, mid) 順比較で破棄し、集計状態を一切変更しないため、
    古いイベントや履歴の再取得が平均を壊さない。
    """

    def __init__(self):
        self.model = None
        self.model_obj = None
        self.samples = []
        self.newest_key = None

    def clear(self):
        self.model = None
        self.model_obj = None
        self.samples = []
        self.newest_key = None

    def update(self, message):
        """1メッセージを取込む。戻り値は集計状態が前進したかどうか。

        メッセージIDと時刻で重複と古い応答を破棄する。新しい応答で実際の
        モデルが変わった場合だけ平均をリセットする。履歴は昇順で渡すこと。
        """
        info = message.get('time') or {}
        created = info.get('created')
        mid = message.get('id')
        if not isinstance(created, (int, float)) \
                or not isinstance(mid, str) or not mid:
            return False
        key = (created, mid)
        if self.newest_key is not None and key < self.newest_key:
            return False
        key_model = model_key(message.get('model'))
        if key_model is None:
            return False
        previous = self.newest_key
        self.newest_key = key
        changed = previous != key
        if key_model != self.model:
            self.model = key_model
            self.model_obj = message.get('model')
            self.samples = []
            changed = True
        sample = message_to_sample(message)
        if sample is not None and self.add_sample(sample):
            changed = True
        return changed

    def add_sample(self, sample):
        for existing in self.samples:
            if existing[1] == sample[1]:
                return False
        self.samples.append(sample)
        self.samples.sort(key=lambda item: (item[0], item[1]))
        if len(self.samples) > AVG_SAMPLES:
            self.samples = self.samples[-AVG_SAMPLES:]
        return True

    def average(self):
        if not self.samples:
            return None
        total = 0
        seconds = 0.0
        for sample in self.samples:
            total += sample[2]
            seconds += sample[3]
        if total <= 0 or seconds <= 0:
            return None
        return total / seconds

    def usage(self):
        if not self.samples:
            return None
        text = '直近%d回の加重平均' % len(self.samples)
        if self.model_obj:
            text += '・' + fmt_model(self.model_obj)
        return text


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
        return self.get('/api/session/%s/message?type=assistant&limit=20&order=desc'
                        % sid).get('data') or []

    def active_sessions(self):
        data = self.get('/api/session/active').get('data')
        return list(data) if isinstance(data, dict) else []

    def recent_sessions(self):
        return self.get('/api/session?limit=20').get('data') or []


class App:
    def __init__(self):
        self.service = Service()
        self.q = queue.Queue()
        self.apiq = queue.Queue()
        self.running = True

        self.target_sid = None
        self.target_child = False
        self.pinned_sid = None
        self.active_sids = frozenset()
        self.recent = []
        self.connected = False
        self.status = 'idle'
        self.agg = Aggregation()
        self.session_tokens = None
        self.last_speed = None
        self.last_seconds = None
        self.last_method = None
        self.last_key = None
        self.session_titles = {}
        self.pending_syncs = set()

        self.root = tk.Tk()
        self.root.title('OpenCode 統計')
        self.root.geometry('330x200+80+80')
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
        self.usage_label = label(8, 'normal', '#9a9a9a', '')
        self.hit_label = label(9, 'normal', '#e2e2e2', 'cache hit(累積): 未計測')
        self.cache_label = label(9, 'normal', '#e2e2e2', 'read: -  write: -  input: -')
        self.last_label = label(8, 'normal', '#9a9a9a', 'last: -')
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
        for sid, title, running, child in self.recent:
            mark = ('[実行中] ' if running else '') + ('[子] ' if child else '') + (title or sid)
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
        self.change_or_refresh(sid)

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
        elif kind == 'session.step.ended' and sid == self.target_sid:
            self.sync_target(sid)

    def api_worker(self):
        last_refresh = 0.0
        while self.running:
            try:
                kind, sid, _ = self.apiq.get(timeout=1.0)
            except queue.Empty:
                now = time.time()
                if self.connected and now - last_refresh >= 15.0:
                    last_refresh = now
                    self.run_job('refresh-all', None)
                continue
            if kind == 'refresh-all':
                last_refresh = time.time()
            self.run_job(kind, sid)

    def run_job(self, kind, sid):
        try:
            if kind == 'refresh-all':
                self.job_refresh_all()
            elif kind == 'target-sync':
                self.pending_syncs.discard(sid)
                self.job_target_sync(sid)
            elif kind == 'execution-check':
                self.job_execution_check(sid)
        except Exception:
            pass

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
            recent.append((sid, title, sid in active, bool(session.get('parentID'))))
            titles[sid] = title
        self.q.put(('session-list', recent, titles))

    def job_target_sync(self, sid):
        info = self.service.session_info(sid)
        messages = self.service.session_messages(sid)
        ordered = list(reversed(messages))
        self.q.put(('target-stats', sid, {
            'title': info.get('title') or 'no title',
            'messages': ordered,
            'tokens': info.get('tokens'),
            'running': sid in self.service.active_sessions(),
            'child': bool(info.get('parentID')),
        }))

    def job_execution_check(self, sid):
        try:
            info = self.service.session_info(sid)
        except Exception:
            return
        if info.get('parentID'):
            return
        self.q.put(('execution-target', sid, None))

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
            self.on_target_stats(param, extra)
        elif kind == 'execution-target':
            self.adopt_running_target(param)
        elif kind == 'execution':
            self.on_execution(param, extra)

    def on_target_stats(self, sid, data):
        if sid != self.target_sid:
            return
        title = data.get('title')
        if title and title != 'no title':
            self.session_titles[sid] = title
        self.target_child = bool(data.get('child'))
        tokens = data.get('tokens')
        if tokens:
            self.session_tokens = tokens
        self.ingest(data.get('messages') or [])
        if data.get('running'):
            self.status = 'running'

    def ingest(self, messages):
        """昇順のメッセージを集計へ取込み、最新確定値の表示を更新する。"""
        for message in messages:
            if self.agg.update(message) and is_completed(message):
                self.apply_last(message)

    def apply_last(self, message):
        stat = calc_stats(message)
        key = stat['key']
        if self.last_key is not None and key < self.last_key:
            return
        self.last_key = key
        self.last_speed = stat['speed']
        self.last_seconds = stat['seconds']
        self.last_method = stat['method']

    def on_execution(self, sid, sub):
        if sub == 'started':
            if sid == self.target_sid:
                self.adopt_running_target(sid)
            elif not self.pinned_sid or sid == self.pinned_sid:
                self.apiq.put(('execution-check', sid, None))
        elif self.target_sid == sid and sub in ('succeeded', 'failed', 'interrupted'):
            self.status = 'idle'

    def sync_target(self, sid):
        """対象セッションの同期要求を登録する。未処理の同一要求は1件にまとめる。"""
        if not sid or sid in self.pending_syncs:
            return
        self.pending_syncs.add(sid)
        self.apiq.put(('target-sync', sid, None))

    def switch_target(self, sid):
        """別セッションへ表示対象を切り替える。同一対象・無効値は無視する。"""
        if not sid or sid == self.target_sid:
            return False
        self.target_sid = sid
        self.target_child = False
        self.reset_stats()
        self.sync_target(sid)
        return True

    def change_or_refresh(self, sid):
        """対象が変われば切替し、同じなら統計を即再取得する。"""
        if not self.switch_target(sid):
            self.sync_target(sid)

    def adopt_running_target(self, sid):
        """実行中セッションを表示対象に採用する。同一対象なら即再取得する。"""
        if self.pinned_sid and sid != self.pinned_sid:
            return
        self.change_or_refresh(sid)
        self.status = 'running'

    def apply_target(self):
        if self.pinned_sid:
            sid = self.pinned_sid
        elif self.target_sid and self.target_sid in self.active_sids:
            sid = self.target_sid
        elif self.active_sids:
            mains = [sid for sid, title, running, child in self.recent
                     if sid in self.active_sids and not child]
            sid = mains[0] if mains else self.target_sid
        elif self.target_sid:
            sid = self.target_sid
        elif self.recent:
            sid = self.recent[0][0]
        else:
            return
        self.change_or_refresh(sid)

    def reset_stats(self):
        self.agg.clear()
        self.session_tokens = None
        self.last_speed = None
        self.last_seconds = None
        self.last_method = None
        self.last_key = None

    def render(self):
        if not self.connected:
            text, color = '未接続', '#ff8e8e'
        elif self.status == 'running':
            text, color = '生成中', '#5ad469'
        else:
            text, color = '待機', '#9a9a9a'
        self.status_label.config(text=text, fg=color)
        avg = self.agg.average()
        if avg is not None:
            self.speed_label.config(text=fmt_speed(avg), fg='#7ec8ff')
            self.usage_label.config(text=self.agg.usage() or '', fg='#9a9a9a')
        elif self.last_method == 'fallback' and self.last_speed is not None:
            self.speed_label.config(text=fmt_speed(self.last_speed), fg='#e8b46a')
            self.usage_label.config(text='参考値(直近1回・完了までの実測)', fg='#e8b46a')
        else:
            self.speed_label.config(text='計測不可', fg='#9a9a9a')
            self.usage_label.config(text='', fg='#9a9a9a')
        tokens = self.session_tokens
        if tokens:
            cache = tokens.get('cache') or {}
            self.hit_label.config(text='cache hit(累積): ' + fmt_hit(calc_hit(tokens)))
            self.cache_label.config(text='read: %s  write: %s  input: %s' % (
                fmt_tok(cache.get('read', 0) or 0),
                fmt_tok(cache.get('write', 0) or 0),
                fmt_tok(tokens.get('input', 0) or 0)))
        else:
            self.hit_label.config(text='cache hit(累積): 未計測')
            self.cache_label.config(text='read: -  write: -  input: -')
        if self.last_speed is not None and self.last_seconds is not None:
            mode = '応答区間' if self.last_method == 'measured' else '完了区間'
            self.last_label.config(text='last: %.1f tok/s (%s %.1fs)' % (
                self.last_speed, mode, self.last_seconds))
        else:
            self.last_label.config(text='last: -')
        self.model_label.config(text='model: ' + fmt_model(self.agg.model_obj))
        sid = self.target_sid
        title = self.session_titles.get(sid) if sid else None
        label = title if title else (sid or '-')
        pin = '[固定] ' if self.pinned_sid == sid else ''
        child = '[子] ' if self.target_child else ''
        self.session_label.config(text='session: ' + pin + child + label)

    def close(self):
        self.running = False
        self.root.destroy()


def main():
    App().root.mainloop()


if __name__ == '__main__':
    main()
