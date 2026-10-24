"""Publish approved HiBOU Threads posts through the official Threads API.

    python3 social/publish.py check                          # read-only health check
    python3 social/publish.py publish                        # send what is due
    python3 social/publish.py publish --dry-run              # show what would be sent
    python3 social/publish.py publish --entry-id 2026-10-30-bedtime

The token comes only from the HIBOU_THREADS_TOKEN environment variable. It is
never printed, written to a file or put in an error message.

Every mode first runs the same pre-flight, in this order, and stops the whole
run on the first failure: the token must belong to @lovehibou (and to the
expected user id once HIBOU_THREADS_USER_ID is set), the token age is
reported, the 24-hour publishing quota must have room, and social/queue.json
must pass validate.py.

An entry can never go out twice:
  1. only `approved` entries inside their time window are considered;
  2. if one of the account's recent posts already starts with the same first
     line, the entry is recorded as published instead of being sent;
  3. the entry is set to `publishing` and that change is pushed to GitHub
     before any API call that posts; if the push fails, nothing is sent;
  4. an entry left in `publishing` by an earlier run is reported, never retried.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate as V  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TAIPEI = timezone(timedelta(hours=8))
API = 'https://graph.threads.net/v1.0'

TOKEN_DAYS = 60
TOKEN_WARN_DAYS, TOKEN_ALARM_DAYS = 10, 3
QUOTA_STOP = 240           # Threads allows 250 API posts per rolling 24 hours
DAILY_POST_CAP = 4         # 3 trend posts + 1 bedtime post per Taipei day; test posts excluded
RECENT_LIMIT = 25          # recent posts read for the duplicate check and the daily cap
POLL_EVERY, POLL_MAX, POLL_FALLBACK = 5, 60, 30
WINDOWS = {
    'morning': timedelta(minutes=90),
    'noon': timedelta(minutes=90),
    'evening': timedelta(minutes=90),
    'bedtime': timedelta(hours=2),
    'test': timedelta(hours=24),
}
BEDTIME_CUTOFF = (23, 30)
BOT_NAME, BOT_EMAIL = 'hibou-social bot', 'noreply@ihibou.com'
ENTRY_ID_RE = re.compile(r'^[A-Za-z0-9._-]+$')


class Abort(Exception):
    """Stop the run: nothing more is sent and the workflow fails."""


class PostError(Exception):
    """A single post could not be created or published."""


class ApiError(Exception):
    def __init__(self, method, path, status, body):
        self.status, self.body = status, body
        # Only the path is shown: the token travels in the query or the form body.
        super().__init__(f'{method} {path} -> {status}: {body[:300]}')

    @property
    def auth(self):
        try:
            err = json.loads(self.body).get('error') or {}
        except (ValueError, AttributeError):
            err = {}
        message = str(err.get('message', self.body)).lower()
        return err.get('code') in (102, 190) or 'expired' in message


def http_api(method, path, params, token):
    params = dict(params)
    params['access_token'] = token
    url, data = API + path, None
    if method == 'GET':
        url += '?' + urllib.parse.urlencode(params)
    else:
        data = urllib.parse.urlencode(params).encode()
    request = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as e:
        raise ApiError(method, path, e.code, e.read().decode('utf-8', 'replace')) from None
    except urllib.error.URLError as e:
        raise ApiError(method, path, 0, str(e.reason)) from None


def http_head(url):
    """Return (status, content type, size) for a public image URL."""
    request = urllib.request.Request(url, method='HEAD')
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.headers.get('Content-Type'), response.headers.get('Content-Length')
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get('Content-Type'), None
    except urllib.error.URLError as e:
        return 0, str(e.reason), None


def first_line(text):
    for line in (text or '').splitlines():
        if line.strip():
            return ' '.join(line.split())
    return ''


def parse_threads_time(value):
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M:%S%z').astimezone(TAIPEI)
    except (TypeError, ValueError):
        return None


def window(entry):
    start = V.parse_publish_at(entry['publish_at'])
    end = start + WINDOWS[entry['slot']]
    if entry['slot'] == 'bedtime':
        end = min(end, start.replace(hour=BEDTIME_CUTOFF[0], minute=BEDTIME_CUTOFF[1], second=0))
    return start, end


class Store:
    """queue.json plus the receipts; in GitHub Actions every change is committed and pushed."""

    def __init__(self, root, commit, branch='main'):
        self.root = Path(root)
        self.queue_path = self.root / 'social' / 'queue.json'
        self.receipts_dir = self.root / 'social' / 'receipts'
        self.commit, self.branch = commit, branch
        self.queue = self._read()

    def _read(self):
        return json.loads(self.queue_path.read_text(encoding='utf-8'))

    def entry(self, entry_id):
        return next((e for e in self.queue if isinstance(e, dict) and e.get('id') == entry_id), None)

    def _apply(self, entry_id, fields, receipt):
        entry = self.entry(entry_id)
        if entry is None:
            raise Abort(f'{entry_id} disappeared from queue.json')
        entry.update(fields)
        self.queue_path.write_text(json.dumps(self.queue, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        if receipt:
            self.receipts_dir.mkdir(parents=True, exist_ok=True)
            path = self.receipts_dir / (receipt['ts'][:7] + '.jsonl')
            with path.open('a', encoding='utf-8') as f:
                f.write(json.dumps(receipt, ensure_ascii=False) + '\n')

    def _git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], capture_output=True, text=True)

    def _push(self, message):
        self._git('add', 'social/queue.json', 'social/receipts')
        if self._git('diff', '--cached', '--quiet').returncode == 0:
            return True
        done = self._git('-c', f'user.name={BOT_NAME}', '-c', f'user.email={BOT_EMAIL}', 'commit', '-q', '-m', message)
        if done.returncode != 0:
            return False
        return self._git('push', '-q', 'origin', f'HEAD:{self.branch}').returncode == 0

    def update(self, entry_id, fields, receipt=None, message='chore(threads): update queue'):
        self._apply(entry_id, fields, receipt)
        if not self.commit:
            return
        for _ in range(3):
            if self._push(message):
                return
            # Someone pushed in between (for example a new trend entry): rebuild this
            # one change on top of the newest main instead of merging JSON by hand.
            self._git('fetch', '-q', 'origin', self.branch)
            self._git('reset', '-q', '--hard', f'origin/{self.branch}')
            self.queue = self._read()
            self._apply(entry_id, fields, receipt)
        raise Abort(f'could not push the queue change for {entry_id}; stopped before doing anything else')


class Runner:
    def __init__(self, root=REPO_ROOT, env=None, api=http_api, clock=None, sleep=time.sleep, head=http_head, commit=None):
        self.env = os.environ if env is None else env
        self.root = Path(root)
        self.token = self.env.get('HIBOU_THREADS_TOKEN', '').strip()
        self._api, self.sleep, self.head = api, sleep, head
        self.now = clock or (lambda: datetime.now(TAIPEI))
        if commit is None:
            commit = self.env.get('GITHUB_ACTIONS') == 'true'
        self.branch = self.env.get('GITHUB_REF_NAME') or 'main'
        self.store = Store(self.root, commit, self.branch)
        self.summary = []
        self.user_id = None
        self.failed = False
        run_id = self.env.get('GITHUB_RUN_ID')
        self.run_id = run_id
        self.run_url = (f"{self.env.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                        f"{self.env.get('GITHUB_REPOSITORY', '')}/actions/runs/{run_id}") if run_id else None

    # ---- output -------------------------------------------------------------

    def say(self, line, level=None):
        print(f'::{level}::{line}' if level else line)
        prefix = {'error': '❌ ', 'warning': '⚠️ '}.get(level, '')
        self.summary.append(prefix + line)

    def write_summary(self, title):
        path = self.env.get('GITHUB_STEP_SUMMARY')
        if not path:
            return
        with open(path, 'a', encoding='utf-8') as f:
            f.write(f'## {title}\n\n' + ''.join(f'- {line}\n' for line in self.summary) + '\n')

    # ---- API ----------------------------------------------------------------

    def call(self, method, path, params=None):
        try:
            return self._api(method, path, params or {}, self.token)
        except ApiError as e:
            if e.auth:
                raise Abort('Threads token 無效或已過期，本次整批跳過，佇列不改。'
                            '請 Otto 重新產生 token，更新 HIBOU_THREADS_TOKEN 與 HIBOU_THREADS_TOKEN_ISSUED') from None
            raise

    def preflight(self):
        """Account, token age, quota. Returns True when the quota still has room."""
        if not self.token:
            raise Abort('HIBOU_THREADS_TOKEN 沒有設定')
        expected = self.env.get('HIBOU_THREADS_USERNAME', '').strip().lstrip('@')
        if not expected:
            raise Abort('HIBOU_THREADS_USERNAME 沒有設定')
        expected_id = self.env.get('HIBOU_THREADS_USER_ID', '').strip()
        me = self.call('GET', '/me', {'fields': 'id,username'})
        username, user_id = me.get('username'), str(me.get('id', ''))
        if username != expected:
            raise Abort(f'帳號不符：預期 @{expected}，實際 @{username}；本次不發任何貼文')
        if expected_id and user_id != expected_id:
            raise Abort(f'帳號不符：預期 @{expected}（id {expected_id}），實際 @{username}（id {user_id}）；本次不發任何貼文')
        self.user_id = user_id
        self.say(f'帳號：@{username}（id {user_id}）')
        if not expected_id:
            self.say('HIBOU_THREADS_USER_ID 還沒設定：請把上面的 id 填進 repo variable', 'warning')

        issued = self.env.get('HIBOU_THREADS_TOKEN_ISSUED', '').strip()
        try:
            left = TOKEN_DAYS - (self.now().date() - date.fromisoformat(issued)).days
        except ValueError:
            self.say('HIBOU_THREADS_TOKEN_ISSUED 沒有設定或格式不是 YYYY-MM-DD，無法計算 token 剩餘天數', 'warning')
        else:
            if left <= TOKEN_ALARM_DAYS:
                self.say(f'token 剩 {left} 天：請 Otto 今天就執行 social/refresh_token.sh', 'error')
                self.failed = True
            elif left <= TOKEN_WARN_DAYS:
                self.say(f'token 剩 {left} 天：請 Otto 執行 social/refresh_token.sh', 'warning')
            else:
                self.say(f'token 剩 {left} 天')

        limit = self.call('GET', f'/{user_id}/threads_publishing_limit',
                          {'fields': 'quota_usage,config,reply_quota_usage,reply_config'})
        row = (limit.get('data') or [{}])[0]
        usage = row.get('quota_usage')
        total = (row.get('config') or {}).get('quota_total')
        replies = row.get('reply_quota_usage')
        reply_total = (row.get('reply_config') or {}).get('quota_total')
        self.say(f'24 小時額度：貼文 {usage}/{total}，回覆 {replies}/{reply_total}')
        if usage is not None and usage >= QUOTA_STOP:
            self.say(f'貼文額度已用 {usage}，達到 {QUOTA_STOP} 的停發線', 'error')
            return False
        return True

    def validate(self):
        problems = V.validate_queue(self.store.queue, self.root)
        for entry_id, problem in problems:
            print(f'::error::{entry_id}: {problem}')
        if problems:
            self.summary.append(f'❌ queue.json 有 {len(problems)} 項驗證錯誤（詳見 log）')
        return not problems

    def recent_posts(self):
        posts = self.call('GET', f'/{self.user_id}/threads',
                          {'fields': 'id,text,timestamp,permalink,media_type', 'limit': RECENT_LIMIT})
        return posts.get('data') or []

    # ---- helpers ------------------------------------------------------------

    def receipt(self, entry, action, **fields):
        row = {
            'ts': self.now().isoformat(timespec='seconds'),
            'entry_id': entry['id'], 'slot': entry.get('slot'), 'action': action,
            'container_id': None, 'media_id': None, 'permalink': None,
            'run_id': self.run_id, 'run_url': self.run_url, 'note': None,
        }
        row.update(fields)
        return row

    def posts_today(self, recent):
        today = self.now().date()
        test_ids = {str(e.get('published_id')) for e in self.store.queue if e.get('slot') == 'test' and e.get('published_id')}
        from_api = 0
        for post in recent:
            when = parse_threads_time(post.get('timestamp'))
            if when and when.date() == today and post.get('media_type') != 'REPOST_FACADE' and str(post.get('id')) not in test_ids:
                from_api += 1
        from_receipts = 0
        path = self.store.receipts_dir / f'{today.isoformat()[:7]}.jsonl'
        if path.exists():
            for line in path.read_text(encoding='utf-8').splitlines():
                row = json.loads(line)
                if row.get('action') == 'post' and row.get('slot') != 'test' and row.get('ts', '')[:10] == today.isoformat():
                    from_receipts += 1
        return max(from_api, from_receipts)

    def wait_ready(self, container):
        waited = 0
        while waited < POLL_MAX:
            self.sleep(POLL_EVERY)
            waited += POLL_EVERY
            try:
                status = self.call('GET', f'/{container}', {'fields': 'status,error_message'})
            except ApiError:
                status = {}
            state = status.get('status')
            if state is None:
                self.sleep(max(POLL_FALLBACK - waited, 0))
                return
            if state == 'FINISHED':
                return
            if state in ('ERROR', 'EXPIRED'):
                raise PostError(f'container {state}: {status.get("error_message", "")}')
        raise PostError(f'container not FINISHED after {POLL_MAX}s')

    def create(self, params):
        container = self.call('POST', f'/{self.user_id}/threads', params).get('id')
        if not container:
            raise PostError('Threads returned no container id')
        self.wait_ready(container)
        media_id = self.call('POST', f'/{self.user_id}/threads_publish', {'creation_id': container}).get('id')
        if not media_id:
            raise PostError(f'Threads returned no media id for container {container}')
        return container, media_id

    def describe(self, entry):
        self.say(f"dry-run：{entry['id']} 會建立 {entry['media_type']} 貼文「{' / '.join(entry['text'][:40].splitlines())}」"
                 f"，主題 {entry.get('topic_tag') or '（無）'}")
        if entry['media_type'] == 'IMAGE':
            status, ctype, size = self.head(entry['image_url'])
            self.say(f'圖片 HEAD：{status}，{ctype}，{size} bytes', None if status == 200 else 'warning')
        if entry.get('self_reply'):
            self.say(f"發完會自己回覆「{entry['self_reply']['text'][:40]}」")

    # ---- modes --------------------------------------------------------------

    def check(self):
        quota_ok = self.preflight()
        valid = self.validate()
        queue, now = self.store.queue, self.now()
        counts = {}
        for e in queue:
            counts[e.get('status')] = counts.get(e.get('status'), 0) + 1
        self.say('佇列：' + ('，'.join(f'{k} {v}' for k, v in sorted(counts.items())) or '空的'))
        for e in queue:
            if e.get('status') == 'publishing':
                self.say(f"{e['id']} 卡在 publishing：請到 @lovehibou 核對有沒有發出，再改成 published 或 failed", 'warning')
            elif e.get('status') == 'approved' and V.parse_publish_at(e.get('publish_at')) and e.get('slot') in WINDOWS:
                if now > window(e)[1]:
                    self.say(f"{e['id']} 已過時間窗還沒發：下一次 publish 會標成 skipped，需要的話請改期", 'warning')
        upcoming = sorted((e for e in queue if e.get('status') == 'approved' and V.parse_publish_at(e.get('publish_at'))
                           and V.parse_publish_at(e['publish_at']) >= now), key=lambda e: e['publish_at'])
        for e in upcoming[:4]:
            self.say(f"接下來：{e['publish_at'][:16]} {e['slot']} {e['id']}")
        for post in self.recent_posts()[:5]:
            self.say(f"最近貼文：{(post.get('timestamp') or '')[:16]} {post.get('permalink', '')} 「{first_line(post.get('text'))[:30]}」")
        return 1 if (self.failed or not valid or not quota_ok) else 0

    def publish(self, dry_run=False, entry_id=None):
        quota_ok = self.preflight()
        if not quota_ok:
            raise Abort('24 小時貼文額度快用完，本次不發')
        if not self.validate():
            raise Abort('queue.json 驗證不通過，本次整批不發')

        target = None
        if entry_id:
            target = self.store.entry(entry_id)
            if target is None:
                raise Abort(f'queue.json 沒有 {entry_id}')
        enabled = self.env.get('THREADS_PUBLISH_ENABLED', '').strip().lower() == 'true'
        if not dry_run and not enabled:
            if target and target.get('slot') == 'test' and target.get('approved_by') == 'otto':
                self.say('THREADS_PUBLISH_ENABLED 不是 true，但這是 Otto 核准的測試貼文，照常發出')
            else:
                dry_run = True
                self.say('THREADS_PUBLISH_ENABLED 不是 true（總開關關著），本次改為 dry-run，不發任何貼文', 'notice')
        if not dry_run and self.branch != 'main':
            raise Abort(f'只能從 main 發文，這次是 {self.branch}')

        for e in self.store.queue:
            if e.get('status') == 'publishing':
                self.say(f"{e['id']} 卡在 publishing（之前的執行中斷）：不重試，請到 @lovehibou 核對後手動改成 published 或 failed", 'warning')

        now = self.now()
        recent = self.recent_posts()
        sent = 0
        for entry in ([target] if target else list(self.store.queue)):
            if entry.get('status') != 'approved':
                if target:
                    self.say(f"{entry['id']} 的狀態是 {entry.get('status')}，不是 approved，不處理")
                continue
            start, end = window(entry)
            if now < start:
                if target:
                    self.say(f"{entry['id']} 還沒到發文時間（{entry['publish_at']}）")
                continue

            line = first_line(entry['text'])
            match = next((p for p in recent if first_line(p.get('text')) == line), None)
            if match:
                note = 'found existing: 帳號上已有同樣第一行的貼文，不重發'
                self.say(f"{entry['id']}：{note} {match.get('permalink', '')}")
                if not dry_run:
                    self.store.update(entry['id'], {
                        'status': 'published', 'published_id': match.get('id'), 'permalink': match.get('permalink'),
                        'published_at': match.get('timestamp'), 'run_id': self.run_id, 'note': note,
                    }, self.receipt(entry, 'skip', media_id=match.get('id'), permalink=match.get('permalink'), note=note),
                        f"chore(threads): {entry['id']} already on Threads")
                continue

            if now > end:
                note = 'missed window'
                self.say(f"{entry['id']} 已過時間窗（{entry['publish_at'][11:16]} 起算），不補發，標成 skipped", 'warning')
                if not dry_run:
                    self.store.update(entry['id'], {'status': 'skipped', 'note': note, 'run_id': self.run_id},
                                      self.receipt(entry, 'skip', note=note), f"chore(threads): skip {entry['id']} (missed window)")
                continue

            if entry['slot'] != 'test':
                today = self.posts_today(recent)
                if today >= DAILY_POST_CAP:
                    self.say(f"{entry['id']} 沒發：今天（台北）已有 {today} 則頂層貼文，達到每日上限 {DAILY_POST_CAP}", 'error')
                    self.failed = True
                    continue

            if dry_run:
                self.describe(entry)
                continue

            self.store.update(entry['id'], {'status': 'publishing', 'run_id': self.run_id}, None,
                              f"chore(threads): publishing {entry['id']}")
            params = {'media_type': entry['media_type'], 'text': entry['text']}
            if entry['media_type'] == 'IMAGE':
                params['image_url'] = entry['image_url']
            if entry.get('link'):
                params['link_attachment'] = entry['link']
            if entry.get('topic_tag'):
                params['topic_tag'] = entry['topic_tag']
            try:
                container, media_id = self.create(params)
            except (ApiError, PostError, Abort) as err:
                note = f'{err}'[:300]
                self.say(f"{entry['id']} 發文失敗：{note}。請到 @lovehibou 核對是否已出現，不會自動重試", 'error')
                self.store.update(entry['id'], {'status': 'failed', 'error': note},
                                  self.receipt(entry, 'fail', note=note), f"chore(threads): {entry['id']} failed")
                self.failed = True
                if isinstance(err, Abort):
                    raise
                continue

            permalink = None
            try:
                permalink = self.call('GET', f'/{media_id}', {'fields': 'permalink'}).get('permalink')
            except (ApiError, Abort) as err:
                self.say(f"{entry['id']} 已發出，但讀不到 permalink：{err}", 'warning')
            fields = {
                'status': 'published', 'container_id': container, 'published_id': media_id, 'permalink': permalink,
                'published_at': self.now().isoformat(timespec='seconds'), 'run_id': self.run_id, 'error': None,
            }
            if entry.get('pin_after_post'):
                fields['needs_pin'] = True
            self.store.update(entry['id'], fields,
                              self.receipt(entry, 'post', container_id=container, media_id=media_id, permalink=permalink,
                                           note='needs_pin' if entry.get('pin_after_post') else None),
                              f"chore(threads): published {entry['id']}")
            self.say(f"已發出 {entry['id']}：{permalink or media_id}")
            recent.insert(0, {'id': media_id, 'text': entry['text'], 'timestamp': self.now().strftime('%Y-%m-%dT%H:%M:%S%z'),
                              'permalink': permalink})
            sent += 1

            reply = entry.get('self_reply')
            if reply:
                try:
                    reply_container, reply_id = self.create({'media_type': 'TEXT', 'text': reply['text'], 'reply_to_id': media_id})
                except (ApiError, PostError, Abort) as err:
                    note = f'{err}'[:300]
                    self.say(f"{entry['id']} 主文已發出，但自己回覆的導流連結失敗：{note}", 'warning')
                    self.store.update(entry['id'], {}, self.receipt(entry, 'fail', note='self_reply: ' + note),
                                      f"chore(threads): {entry['id']} self-reply failed")
                else:
                    self.store.update(entry['id'], {'reply_id': reply_id},
                                      self.receipt(entry, 'self_reply', container_id=reply_container, media_id=reply_id),
                                      f"chore(threads): {entry['id']} self-reply")
                    self.say(f"{entry['id']} 已自己回覆導流連結")

        if sent == 0 and not self.failed:
            self.say('這次沒有要發的貼文' if not dry_run else 'dry-run 結束，沒有發任何貼文')
        return 1 if self.failed else 0


def main(argv=None, **runner_args):
    parser = argparse.ArgumentParser(description='Publish approved HiBOU Threads posts.')
    parser.add_argument('mode', choices=('check', 'publish'))
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--entry-id')
    args = parser.parse_args(argv)
    if args.entry_id and not ENTRY_ID_RE.match(args.entry_id):
        print('::error::--entry-id may contain only letters, digits, . _ -')
        return 1
    runner = Runner(**runner_args)
    title = 'Threads 檢查' if args.mode == 'check' else ('Threads 發文（dry-run）' if args.dry_run else 'Threads 發文')
    try:
        return runner.check() if args.mode == 'check' else runner.publish(args.dry_run, args.entry_id)
    except Abort as e:
        runner.say(str(e), 'error')
        return 1
    finally:
        runner.write_summary(title)


if __name__ == '__main__':
    sys.exit(main())
