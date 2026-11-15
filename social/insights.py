"""Read-only Threads insights pull for published @lovehibou posts.

    python3 social/insights.py

The token comes only from the HIBOU_THREADS_TOKEN environment variable. It is
never printed, written to a file, or put in an error message.

This script never publishes, edits, or changes social/queue.json statuses.
It only writes social/metrics/YYYY-MM-DD.jsonl (Taipei date) and, when run in
GitHub Actions, commits that file.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

REPO_ROOT = Path(__file__).resolve().parent.parent
TAIPEI = timezone(timedelta(hours=8))
API = 'https://graph.threads.net/v1.0'
INSIGHTS_SCOPE = 'threads_manage_insights'
METRICS = 'views,likes,replies,reposts,quotes,shares'
BOT_NAME, BOT_EMAIL = 'hibou-social bot', 'noreply@ihibou.com'
# Media / queue ids are digits or the documented entry-id alphabet.
ID_RE = re.compile(r'^[A-Za-z0-9._-]+$')
UTM_RE = re.compile(
    r'https?://(?:www\.)?ihibou\.com[^\s\"\'<>]*?[?&]utm_campaign=([A-Za-z0-9_.-]+)',
    re.I,
)


class Abort(Exception):
    """Stop the run. The workflow fails."""


class MissingInsightsScope(Exception):
    """The token cannot read insights. The workflow exits cleanly and writes nothing."""


class ApiError(Exception):
    def __init__(self, method, path, status, body):
        self.method, self.path, self.status, self.body = method, path, status, body
        # Only the path is shown: the token travels in the query or the form body.
        super().__init__(f'{method} {path} -> {status}: {redact(body)[:300]}')


def redact(value):
    text = value if isinstance(value, str) else str(value)
    text = re.sub(r'access_token=[^&\s]+', 'access_token=REDACTED', text, flags=re.I)
    text = re.sub(r'input_token=[^&\s]+', 'input_token=REDACTED', text, flags=re.I)
    return text


def mask_token(token):
    if token and os.environ.get('GITHUB_ACTIONS') == 'true':
        sys.stdout.write(f'::add-mask::{token}\n')
        sys.stdout.flush()


def missing_insights_scope(error):
    """True when the API refused because threads_manage_insights is missing."""
    payload = error.body if isinstance(error, ApiError) else error
    if isinstance(error, ApiError):
        try:
            payload = json.loads(error.body)
        except (ValueError, TypeError):
            payload = {'error': {'message': error.body, 'code': None}}
    err = payload.get('error') if isinstance(payload, dict) else {}
    if not isinstance(err, dict):
        err = {}
    message = str(err.get('message') or err.get('error_user_msg') or err.get('error_user_title') or payload)
    blob = message.lower()
    code = err.get('code')
    if INSIGHTS_SCOPE in blob or INSIGHTS_SCOPE.replace('_', ' ') in blob:
        return True
    if code in (10, 200, 294) and any(word in blob for word in ('permission', 'scope', 'insights', 'oauth')):
        return True
    return False


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


def extract_utm_campaign(*texts):
    for text in texts:
        if not text:
            continue
        if isinstance(text, dict):
            found = extract_utm_campaign(text.get('text'), text.get('link'))
            if found:
                return found
            continue
        match = UTM_RE.search(str(text))
        if match:
            return match.group(1)
        try:
            parsed = urlparse(str(text))
            values = parse_qs(parsed.query).get('utm_campaign') or []
            if len(values) == 1 and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', values[0]):
                return values[0]
        except ValueError:
            continue
    return None


def published_targets(queue, receipts):
    """Unique media ids from queue published_id and receipt published_id/media_id."""
    by_media = {}
    queue_by_id = {}
    for entry in queue or []:
        if not isinstance(entry, dict):
            continue
        entry_id = entry.get('id')
        if isinstance(entry_id, str):
            queue_by_id[entry_id] = entry
        media_id = entry.get('published_id')
        if media_id and ID_RE.fullmatch(str(media_id)):
            by_media[str(media_id)] = entry

    for row in receipts or []:
        if not isinstance(row, dict):
            continue
        media_id = row.get('published_id') or row.get('media_id')
        if not media_id or not ID_RE.fullmatch(str(media_id)):
            continue
        media_id = str(media_id)
        if media_id in by_media:
            continue
        entry = queue_by_id.get(row.get('entry_id')) or {}
        by_media[media_id] = {
            'id': row.get('entry_id') or entry.get('id'),
            'topic_tag': entry.get('topic_tag'),
            'text': entry.get('text'),
            'self_reply': entry.get('self_reply'),
        }

    targets = []
    for media_id, entry in by_media.items():
        targets.append({
            'media_id': media_id,
            'queue_id': entry.get('id'),
            'topic_tag': entry.get('topic_tag'),
            'utm_campaign': extract_utm_campaign(entry.get('text'), entry.get('self_reply')),
        })
    targets.sort(key=lambda row: (row['queue_id'] or '', row['media_id']))
    return targets


def parse_metrics(payload):
    metrics = {}
    for item in payload.get('data') or []:
        name = item.get('name')
        if name not in METRICS.split(','):
            continue
        total = item.get('total_value')
        if isinstance(total, dict) and 'value' in total:
            metrics[name] = total['value']
        elif 'values' in item and item['values']:
            last = item['values'][-1]
            metrics[name] = last.get('value') if isinstance(last, dict) else last
        elif 'value' in item:
            metrics[name] = item['value']
    return metrics


def load_receipts(root):
    rows = []
    folder = Path(root) / 'social' / 'receipts'
    if not folder.is_dir():
        return rows
    for path in sorted(folder.glob('*.jsonl')):
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def debug_scopes(api, token):
    """Best-effort token permission probe. Never raises on failure."""
    try:
        payload = api('GET', '/debug_token', {'input_token': token}, token)
    except ApiError:
        return None
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    scopes = data.get('scopes') or data.get('granular_scopes') or []
    names = []
    for item in scopes:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and item.get('scope'):
            names.append(item['scope'])
    return names


class Runner:
    def __init__(self, root=REPO_ROOT, env=None, api=http_api, clock=None, commit=None):
        self.root = Path(root)
        self.env = env if env is not None else os.environ
        self.api = api
        self.clock = clock or (lambda: datetime.now(TAIPEI))
        self.token = (self.env.get('HIBOU_THREADS_TOKEN') or '').strip()
        if commit is None:
            commit = self.env.get('GITHUB_ACTIONS') == 'true'
        self.commit = commit
        self.branch = self.env.get('GITHUB_REF_NAME') or 'main'

    def now(self):
        return self.clock()

    def say(self, line, level=None):
        prefix = {'error': '::error::', 'warning': '::warning::', 'notice': '::notice::'}.get(level, '')
        print(f'{prefix}{line}', flush=True)

    def call(self, method, path, params=None):
        try:
            return self.api(method, path, params or {}, self.token)
        except ApiError as e:
            if missing_insights_scope(e):
                raise MissingInsightsScope(str(e)) from None
            raise

    def write_nothing(self):
        """Do not leave a partial metrics file behind."""
        return None

    def metrics_path(self, when=None):
        day = (when or self.now()).astimezone(TAIPEI).date().isoformat()
        return self.root / 'social' / 'metrics' / f'{day}.jsonl'

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], capture_output=True, text=True)

    def commit_metrics(self, path, message):
        if not self.commit:
            return
        self.git('add', '--', str(path.relative_to(self.root)))
        if self.git('diff', '--cached', '--quiet').returncode == 0:
            self.say('metrics file unchanged; nothing to commit')
            return
        done = self.git('-c', f'user.name={BOT_NAME}', '-c', f'user.email={BOT_EMAIL}', 'commit', '-q', '-m', message)
        if done.returncode != 0:
            raise Abort(redact(done.stderr or done.stdout or 'git commit failed'))
        for _ in range(3):
            pushed = self.git('push', '-q', 'origin', f'HEAD:{self.branch}')
            if pushed.returncode == 0:
                return
            pull = self.git('pull', '--rebase', '-q', 'origin', self.branch)
            if pull.returncode != 0:
                break
        raise Abort('could not push the insights file')

    def run(self):
        mask_token(self.token)
        if not self.token:
            raise Abort('HIBOU_THREADS_TOKEN 沒有設定')
        me = self.call('GET', '/me', {'fields': 'id,username'})
        username = me.get('username') or ''
        self.say(f"帳號 {username or '(unknown)'} id={me.get('id') or '(unknown)'}")
        scopes = debug_scopes(self.call if False else self.api, self.token)
        # debug_scopes uses the raw API so a debug_token failure is not fatal
        # and is not treated as a missing-insights-scope error by itself.
        if scopes is not None:
            self.say('token scopes: ' + (', '.join(sorted(set(scopes))) or '(none reported)'))
            if scopes and INSIGHTS_SCOPE not in scopes:
                raise MissingInsightsScope(INSIGHTS_SCOPE)
        queue_path = self.root / 'social' / 'queue.json'
        queue = json.loads(queue_path.read_text(encoding='utf-8'))
        targets = published_targets(queue, load_receipts(self.root))
        self.say(f'要讀取 Insights 的貼文 {len(targets)} 則')
        fetched_at = self.now().astimezone(TAIPEI).isoformat(timespec='seconds')
        rows = []
        try:
            for target in targets:
                payload = self.call(
                    'GET',
                    f"/{target['media_id']}/insights",
                    {'metric': METRICS},
                )
                rows.append({
                    'media_id': target['media_id'],
                    'queue_id': target['queue_id'],
                    'topic_tag': target['topic_tag'],
                    'utm_campaign': target['utm_campaign'],
                    'metrics': parse_metrics(payload),
                    'fetched_at': fetched_at,
                })
        except MissingInsightsScope:
            self.write_nothing()
            raise
        path = self.metrics_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
        self.say(f'已寫入 {path.relative_to(self.root)}（{len(rows)} 則）')
        self.commit_metrics(path, f"chore(threads): record insights for {path.stem}")
        return path


def main(argv=None):
    try:
        Runner().run()
        return 0
    except MissingInsightsScope:
        print(
            f'::notice::此次 token 沒有 {INSIGHTS_SCOPE} 權限，無法讀取 Insights。沒有寫入任何檔案。'
            '請在 Meta 應用程式補上這個權限並更新 HIBOU_THREADS_TOKEN。',
            flush=True,
        )
        return 0
    except Abort as e:
        print(f'::error::{e}', flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
