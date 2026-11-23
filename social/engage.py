"""Reply to @lovehibou and, when allowed, comment on parenting posts.

    python3 social/engage.py
    python3 social/engage.py --dry-run

The token comes only from HIBOU_THREADS_TOKEN. It is never printed, written to a
file, or put in an error message.

Live posting also requires the repository variable THREADS_ENGAGE_ENABLED=true
and a run from main. Anything else stays a dry-run: drafts are shown, nothing
is sent, and the dedupe log is not updated.

Phase A reads replies and mentions from the last 14 days. Phase B (keyword
comments) runs only when the token includes threads_keyword_search, unless
THREADS_ENGAGE_DISCOVERY=false.

Copy is checked by engage_copy.validate_draft before any send. Gemini is used
when GEMINI_API_KEY or GOOGLE_API_KEY is set; otherwise a fixed colloquial
template is used. Gift-date, price, and complaint replies stay on fixed lines.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engage_copy as C  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TAIPEI = timezone(timedelta(hours=8))
API = 'https://graph.threads.net/v1.0'
BOT_NAME, BOT_EMAIL = 'hibou-social bot', 'noreply@ihibou.com'
MEDIA_ID_RE = re.compile(r'^\d{5,30}$')
POLL_EVERY, POLL_MAX = 2, 20
FIELDS = (
    'id,text,username,timestamp,permalink,replied_to,is_reply,'
    'is_reply_owned_by_me,hide_status,has_replies,media_type,root_post'
)
SEARCH_FIELDS = 'id,text,media_type,permalink,timestamp,username,has_replies,is_quote_post,is_reply'
STOP_LABEL = {
    'rate_limit': '速率限制',
    'spam': '垃圾訊息警告',
    'hidden': '留言被隱藏',
    'policy': '社群政策',
}


class Abort(Exception):
    """Stop the run. Nothing further is sent."""


class StopRun(Abort):
    """Threads asked us to stop: rate limit, spam, hidden comment, or policy."""

    def __init__(self, reason, detail):
        self.reason, self.detail = reason, detail
        super().__init__(f'{STOP_LABEL.get(reason, reason)}：{detail}')


class MissingScope(Exception):
    def __init__(self, scope):
        self.scope = scope
        super().__init__(scope or 'permission')


class PostError(Exception):
    """This one reply failed before it was confirmed."""


class LlmError(Exception):
    """The copy model could not be used. The template takes over."""


class ApiError(Exception):
    def __init__(self, method, path, status, body):
        self.method, self.path, self.status, self.body = method, path, status, body
        super().__init__(f'{method} {path} -> {status}: {redact(body)[:300]}')

    @property
    def payload(self):
        try:
            data = json.loads(self.body)
        except (ValueError, TypeError):
            return {}
        err = data.get('error') if isinstance(data, dict) else None
        return err if isinstance(err, dict) else {}

    @property
    def auth(self):
        message = str(self.payload.get('message', self.body)).lower()
        return self.payload.get('code') in (102, 190) or 'expired' in message or 'session has expired' in message


def redact(value):
    text = value if isinstance(value, str) else str(value)
    for name in ('access_' + 'token', 'input_' + 'token', 'api_' + 'key'):
        text = re.sub(name + r'=[^&\s]+', name + '=REDACTED', text, flags=re.I)
    text = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}', r'\1REDACTED', text)
    return text


def mask_secret(value, env):
    if value and env.get('GITHUB_ACTIONS') == 'true':
        sys.stdout.write(f'::add-mask::{value}\n')
        sys.stdout.flush()


def stop_reason(error):
    """Return rate_limit, spam, hidden, or policy when the run must stop."""
    message = str(error.payload.get('message') or error.body or '').lower()
    code = error.payload.get('code')
    if error.status == 429 or code in (4, 17, 32, 613, 80001, 80002, 80006):
        return 'rate_limit'
    if 'rate limit' in message or 'too many calls' in message or 'please wait' in message:
        return 'rate_limit'
    if 'spam' in message:
        return 'spam'
    if 'hidden comment' in message or 'reply is hidden' in message or 'comment is hidden' in message or 'hushed' in message:
        return 'hidden'
    if code in (368, 506) or 'community standard' in message or 'violat' in message or 'action blocked' in message or 'temporarily blocked' in message:
        return 'policy'
    return None


def permission_scope(error):
    message = str(error.payload.get('message') or error.body or '').lower()
    for scope in C.KNOWN_SCOPES:
        if scope in message or scope.replace('_', ' ') in message:
            return scope
    code = error.payload.get('code')
    if code in (10, 200, 294) and any(word in message for word in ('permission', 'scope', 'oauth')):
        return 'permission'
    return None


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


def gemini_complete(prompt, key, model):
    safe_model = re.sub(r'[^A-Za-z0-9_.:-]', '', model) or 'gemini-2.5-flash'
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{safe_model}:generateContent'
    body = json.dumps({
        'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.8, 'maxOutputTokens': 2048},
    }).encode()
    request = urllib.request.Request(url, data=body, method='POST', headers={
        'Content-Type': 'application/json',
        'x-goog-api-key': key,
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = (json.loads(e.read() or b'{}').get('error') or {}).get('message') or ''
        except (ValueError, AttributeError, OSError):
            pass
        raise LlmError(redact(f'gemini http {e.code} {detail}'.strip())[:200]) from None
    except urllib.error.URLError:
        raise LlmError('gemini network') from None
    candidates = payload.get('candidates') or []
    parts = []
    if candidates and isinstance(candidates[0], dict):
        parts = ((candidates[0].get('content') or {}).get('parts')) or []
    texts = [part.get('text', '') for part in parts if isinstance(part, dict) and part.get('text')]
    if not texts:
        reason = candidates[0].get('finishReason') if candidates and isinstance(candidates[0], dict) else None
        blocked = (payload.get('promptFeedback') or {}).get('blockReason')
        raise LlmError(f'gemini empty ({reason or blocked or "no candidates"})')
    return '\n'.join(texts).strip()


def resolve_provider(env):
    choice = (env.get('HIBOU_ENGAGE_LLM_PROVIDER') or 'auto').strip().lower()
    key = (env.get('GEMINI_API_KEY') or env.get('GOOGLE_API_KEY') or '').strip()
    model = (env.get('HIBOU_ENGAGE_GEMINI_MODEL') or 'gemini-2.5-flash').strip() or 'gemini-2.5-flash'
    if choice not in ('auto', 'gemini', 'template'):
        choice = 'template'
    if choice == 'template' or not key:
        return 'template', '', model, choice == 'gemini' and not key
    return 'gemini', key, model, False


def unwrap_draft(text):
    raw = (text or '').strip()
    if len(raw) >= 2 and raw[0] in '「"' and raw[-1] in '」"':
        raw = raw[1:-1].strip()
    return raw


def as_bool(value):
    if isinstance(value, str):
        return value.strip().lower() == 'true'
    return value is True


def username_of(item):
    return str((item or {}).get('username') or '').strip().lstrip('@')


def media_id_of(item):
    value = str((item or {}).get('id') or '')
    return value if MEDIA_ID_RE.fullmatch(value) else ''


def parent_id_of(item):
    parent = (item or {}).get('replied_to')
    if isinstance(parent, dict):
        parent = parent.get('id')
    value = str(parent or '')
    return value if MEDIA_ID_RE.fullmatch(value) else ''


def is_ours(item, username):
    if as_bool(item.get('is_reply_owned_by_me')):
        return True
    name = username_of(item)
    return bool(name) and name.lower() == username.lower()


class Runner:
    def __init__(self, root=REPO_ROOT, env=None, api=http_api, clock=None, sleep=time.sleep, commit=None, complete=None):
        self.root = Path(root)
        self.env = os.environ if env is None else env
        self._api = api
        self.sleep = sleep
        self.clock = clock or (lambda: datetime.now(TAIPEI))
        self._complete = complete
        self.token = (self.env.get('HIBOU_THREADS_TOKEN') or '').strip()
        self.provider, self.gemini_key, self.model, self.provider_fallback = resolve_provider(self.env)
        if commit is None:
            commit = self.env.get('GITHUB_ACTIONS') == 'true'
        self.commit = commit
        self.branch = self.env.get('GITHUB_REF_NAME') or 'main'
        self._offset = timedelta(0)
        self.summary = []
        self.rows = []
        self.history = []
        self.user_id = None
        self.username = None
        self.scopes = None
        self.dry_run = True
        self.preview_only = False
        self.last_post_at = None
        self.reply_count = 0
        self.comment_count = 0
        self.skip_tallies = {}
        self.discovery_on = False
        self._halt = None
        self.failed = False
        self.llm_ready = self.provider == 'gemini'
        run_id = self.env.get('GITHUB_RUN_ID')
        self.run_id = run_id
        self.run_url = None
        if run_id:
            server = self.env.get('GITHUB_SERVER_URL', 'https://github.com')
            repo = self.env.get('GITHUB_REPOSITORY', '')
            self.run_url = f'{server}/{repo}/actions/runs/{run_id}'

    def now(self):
        return self.clock() + self._offset

    def pause(self, seconds):
        seconds = float(seconds)
        if seconds <= 0:
            return
        before = self.clock()
        self.sleep(seconds)
        if self.clock() <= before:
            self._offset += timedelta(seconds=seconds)

    def scrub(self, text):
        text = redact(text)
        for secret in (self.token, self.gemini_key):
            if secret and secret in text:
                text = text.replace(secret, 'REDACTED')
        return text

    def say(self, line, level=None):
        line = self.scrub(str(line))
        prefix = {'error': '::error::', 'warning': '::warning::', 'notice': '::notice::'}.get(level, '')
        print(f'{prefix}{line}', flush=True)
        mark = {'error': '❌ ', 'warning': '⚠️ '}.get(level, '')
        self.summary.append(mark + line)

    def write_summary(self):
        path = self.env.get('GITHUB_STEP_SUMMARY')
        if not path:
            return
        title = 'Threads 互動（dry-run）' if self.dry_run or self.preview_only else 'Threads 互動'
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(f'## {title}\n\n')
            for line in self.summary:
                handle.write(f'- {line}\n')
            handle.write('\n')

    def write_preview(self):
        path = self.env.get('ENGAGEMENT_PREVIEW_PATH')
        if not path or not self.rows:
            return
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in self.rows), encoding='utf-8')

    def call(self, method, path, params=None):
        try:
            return self._api(method, path, params or {}, self.token)
        except ApiError as error:
            if error.auth:
                raise Abort('Threads token 無效或已過期，這輪停止。請 Otto 重新產生 token，'
                            '更新 HIBOU_THREADS_TOKEN 與 HIBOU_THREADS_TOKEN_ISSUED') from None
            reason = stop_reason(error)
            if reason:
                raise StopRun(reason, self.scrub(str(error))[:240]) from None
            scope = permission_scope(error)
            if scope:
                raise MissingScope(scope) from None
            raise

    def paged(self, path, params, pages=2):
        params = dict(params)
        rows = []
        for _ in range(pages):
            payload = self.call('GET', path, params)
            data = payload.get('data') if isinstance(payload, dict) else None
            if isinstance(data, list):
                rows.extend(item for item in data if isinstance(item, dict))
            after = ((payload.get('paging') or {}).get('cursors') or {}).get('after') if isinstance(payload, dict) else None
            if not after:
                break
            params['after'] = after
        return rows

    def fetch_scopes(self):
        try:
            payload = self._api('GET', '/debug_token', {'input_token': self.token}, self.token)
        except ApiError:
            return None
        data = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return None
        if data.get('is_valid') is False:
            raise Abort('Threads token 無效或已過期，這輪停止。請 Otto 重新產生 token')
        names = []
        for item in data.get('scopes') or data.get('granular_scopes') or []:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict) and item.get('scope'):
                names.append(str(item['scope']))
        return set(names)

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], capture_output=True, text=True)

    def commit_log(self, path):
        if not self.commit:
            return
        relative = str(path.relative_to(self.root))
        message = f'chore(threads): log engagement {path.stem}'
        self.git('add', '--', relative)
        if self.git('diff', '--cached', '--quiet').returncode == 0:
            return
        done = self.git('-c', f'user.name={BOT_NAME}', '-c', f'user.email={BOT_EMAIL}', 'commit', '-q', '-m', message)
        if done.returncode != 0:
            raise Abort(self.scrub(done.stderr or done.stdout or 'git commit failed'))
        for _ in range(3):
            pushed = self.git('push', '-q', 'origin', f'HEAD:{self.branch}')
            if pushed.returncode == 0:
                return
            pull = self.git('pull', '--rebase', '-q', 'origin', self.branch)
            if pull.returncode != 0:
                self.git('rebase', '--abort')
                break
        raise Abort('互動紀錄推不回 GitHub，為避免重複留言，這輪停止')

    def log_path(self, ts):
        day = ts[:10]
        return self.root / 'social' / 'engagement' / f'{day}.jsonl'

    def persist(self, row):
        self.rows.append(row)
        if row.get('dry_run'):
            return
        path = self.log_path(row['ts'])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')
        self.commit_log(path)

    def load_history(self):
        folder = self.root / 'social' / 'engagement'
        paths = sorted(folder.glob('*.jsonl')) if folder.is_dir() else []
        self.history = C.load_log_rows(paths)
        receipts = []
        receipt_dir = self.root / 'social' / 'receipts'
        if receipt_dir.is_dir():
            receipts = C.load_log_rows(sorted(receipt_dir.glob('*.jsonl')))
        self.last_post_at = C.latest_outbound_at(self.history + receipts)

    def handled(self):
        return C.handled_source_ids(self.history + self.rows)

    def within_lookback(self, item):
        when = C.parse_iso(item.get('timestamp'))
        if when is None:
            return False
        age = self.now() - when
        return timedelta(0) <= age <= timedelta(days=C.LOOKBACK_DAYS)

    def base_row(self, kind_type, item, text, reason, needs_human):
        return {
            'ts': self.now().isoformat(timespec='seconds'),
            'type': kind_type,
            'source_url': item.get('permalink') or '',
            'source_id': item['source_id'],
            'other_account': item.get('username') or '',
            'text': text or '',
            'media_id': None,
            'permalink': None,
            'root_url': item.get('root_url') or '',
            'action': None,
            'reason': reason,
            'needs_human': bool(needs_human),
            'dry_run': bool(self.dry_run or self.preview_only),
            'run_id': self.run_id,
            'run_url': self.run_url,
            'note': None,
        }

    def tally(self, reason):
        self.skip_tallies[reason] = self.skip_tallies.get(reason, 0) + 1

    def flag(self, item, reason):
        label = C.REASON_LABEL.get(reason, reason)
        link = item.get('permalink') or item.get('source_id')
        self.say(f"需要人工跟進 @{item.get('username') or 'unknown'}（{label}）{link}", 'warning')

    def wait_to_send(self):
        for _ in range(4):
            resume = C.quiet_resume_at(self.now())
            if resume is not None and resume > self.now():
                self.say(f"避開固定發文時段，等到 {resume.strftime('%H:%M')} 再送")
                self.pause((resume - self.now()).total_seconds())
                continue
            remain = C.gap_seconds(self.last_post_at, self.now())
            if remain > 0:
                self.pause(remain)
                continue
            return
        raise Abort('無法避開發文時段，這輪停止')

    def wait_ready(self, container):
        waited = 0
        while waited < POLL_MAX:
            self.pause(POLL_EVERY)
            waited += POLL_EVERY
            try:
                status = self.call('GET', f'/{container}', {'fields': 'status,error_message'})
            except StopRun:
                raise
            except ApiError:
                continue
            state = status.get('status')
            if state == 'FINISHED' or state is None and waited >= 4:
                return
            if state in ('ERROR', 'EXPIRED'):
                raise PostError(f'container {state}: {status.get("error_message") or ""}')
        raise PostError(f'container not FINISHED after {POLL_MAX}s')

    def publish_text(self, text, reply_to_id):
        if not MEDIA_ID_RE.fullmatch(str(reply_to_id)):
            raise PostError('refusing a non-numeric reply target')
        try:
            created = self.call('POST', f'/{self.user_id}/threads', {
                'media_type': 'TEXT',
                'text': text,
                'reply_to_id': reply_to_id,
            })
        except StopRun:
            raise
        except ApiError as error:
            raise PostError(self.scrub(str(error))[:240]) from None
        container = str((created or {}).get('id') or '')
        if not container:
            raise PostError('Threads returned no container id')
        try:
            self.wait_ready(container)
            published = self.call('POST', f'/{self.user_id}/threads_publish', {'creation_id': container})
        except StopRun as error:
            error.detail = f'{error.detail}（container {container}，請先核對有沒有發出）'
            raise
        except ApiError as error:
            raise PostError(f'container {container}: {self.scrub(str(error))[:180]}') from None
        media_id = str((published or {}).get('id') or '')
        if not media_id:
            raise PostError(f'container {container} returned no media id')
        permalink = None
        try:
            permalink = self.call('GET', f'/{media_id}', {'fields': 'permalink'}).get('permalink')
        except StopRun as error:
            self.say(f'已送出 {media_id}，接著碰到限制，不再繼續', 'error')
            self._halt = error
        except (ApiError, MissingScope):
            self.say(f'已送出 {media_id}，但讀不到 permalink', 'warning')
        return media_id, permalink

    def complete(self, prompt):
        if self._complete is not None:
            return self._complete(prompt)
        return gemini_complete(prompt, self.gemini_key, self.model)

    def compose(self, kind, source_text, source_id, subtype, context=''):
        fixed = C.fixed_text(kind, source_id, subtype)
        if fixed is not None:
            problems = C.validate_draft(fixed)
            return (fixed, []) if not problems else (None, problems)
        if self.llm_ready:
            prompt = C.build_prompt(kind, source_text, C.concrete_detail(source_text), context)
            for _ in range(3):
                try:
                    text = unwrap_draft(self.complete(prompt))
                except LlmError as error:
                    self.llm_ready = False
                    self.say(f'Gemini 這次無法產生文案（{self.scrub(str(error))}），改用固定句型', 'warning')
                    break
                problems = C.validate_draft(text)
                if not problems:
                    return text, []
                prompt += '\n上一則沒通過：' + '、'.join(problems) + '\n請只重寫留言本身。'
        text = C.template_text(kind, source_text, source_id, subtype, context)
        problems = C.validate_draft(text)
        if problems:
            return None, problems
        return text, []

    def send(self, kind_type, item, text, reason, human):
        row = self.base_row(kind_type, item, text, reason, human)
        label = C.REASON_LABEL.get(reason, reason)
        who = item.get('username') or 'unknown'
        if self.dry_run or self.preview_only:
            row['action'] = 'dry_run'
            row['dry_run'] = True
            self.persist(row)
            self.say(f"dry-run {kind_type} @{who}（{label}）：{text} ← {item.get('permalink') or item['source_id']}")
            if human:
                self.flag(item, reason)
            return True
        self.wait_to_send()
        try:
            media_id, permalink = self.publish_text(text, item['source_id'])
        except StopRun as error:
            row['action'] = 'stopped'
            row['dry_run'] = False
            row['reason'] = error.reason
            row['note'] = error.detail[:300]
            self.persist(row)
            raise
        except MissingScope as error:
            self.preview_only = True
            row['action'] = 'dry_run'
            row['dry_run'] = True
            self.persist(row)
            self.say(f'送出時發現缺少 {error.scope}，這則和後面的都只模擬', 'notice')
            self.say(f"dry-run {kind_type} @{who}（{label}）：{text} ← {item.get('permalink') or item['source_id']}")
            return True
        except PostError as error:
            uncertain = str(error).startswith('container ')
            row['action'] = 'skipped' if uncertain else 'stopped'
            row['dry_run'] = False
            row['reason'] = 'uncertain' if uncertain else 'error'
            row['note'] = str(error)[:300]
            if uncertain:
                self.persist(row)
                self.say(f"@{who} 這則可能已建立容器但未確認發出，不再自動重試：{error}", 'warning')
                return False
            self.persist(row)
            self.say(f"@{who} 送出失敗，下一輪再試：{error}", 'warning')
            return False
        row['action'] = 'posted'
        row['dry_run'] = False
        row['media_id'] = media_id
        row['permalink'] = permalink
        self.persist(row)
        self.last_post_at = self.now()
        verb = '回覆' if kind_type == 'reply' else '留言'
        self.say(f'已{verb} @{who}（{label}）：{text} → {permalink or media_id}')
        if human:
            self.flag(item, reason)
        if self._halt is not None:
            raise self._halt
        return True

    def discard(self, kind_type, item, reason, human=False):
        row = self.base_row(kind_type, item, '', reason, human)
        if self.dry_run or self.preview_only:
            row['action'] = 'dry_run_skip'
            row['dry_run'] = True
        else:
            row['action'] = 'skipped'
            row['dry_run'] = False
        self.persist(row)
        label = C.REASON_LABEL.get(reason, reason)
        self.say(f"略過 @{item.get('username') or 'unknown'}（{label}）{item.get('permalink') or item['source_id']}")
        if human:
            self.flag(item, reason)

    def consider_reply(self, item):
        if item['source_id'] in self.handled():
            return
        if self.reply_count >= C.REPLY_RUN_CAP:
            return
        kind = C.classify_inbound(item.get('text'))
        name = kind['class']
        if name == 'empty':
            return
        if not C.should_reply(name):
            self.discard('reply', item, name, human=C.needs_human(name))
            return
        text, problems = self.compose(name, item.get('text'), item['source_id'], kind.get('subtype'), item.get('context') or '')
        if text is None:
            self.discard('reply', item, 'guardrail')
            self.say(f"文案未過，不送出：{'、'.join(problems)}", 'warning')
            return
        self.reply_count += 1
        self.send('reply', item, text, name, C.needs_human(name))

    def absorb(self, root_id, root_url, into, root_text=''):
        try:
            items = self.paged(f'/{root_id}/conversation', {'fields': FIELDS, 'limit': '50'})
        except MissingScope:
            raise
        except ApiError as error:
            self.say(f'讀不到 {root_id} 的留言：{self.scrub(str(error))[:160]}', 'warning')
            return
        answered = set()
        for item in items:
            if is_ours(item, self.username):
                parent = parent_id_of(item)
                if parent:
                    answered.add(parent)
        for item in items:
            source_id = media_id_of(item)
            if not source_id or source_id in into or source_id in answered:
                continue
            if not self.within_lookback(item) or is_ours(item, self.username):
                continue
            when = C.parse_iso(item.get('timestamp'))
            record = {
                'source_id': source_id,
                'username': username_of(item),
                'text': item.get('text') or '',
                'permalink': item.get('permalink') or '',
                'root_url': root_url or '',
                'context': root_text or '',
                'hide_status': item.get('hide_status'),
                'ts': when.isoformat() if when else '',
            }
            into[source_id] = record

    def collect_replies(self):
        if self.scopes is not None and 'threads_read_replies' not in self.scopes:
            self.say('token 沒有 threads_read_replies，這輪不讀留言', 'notice')
            return []
        since = str(int((self.now() - timedelta(days=C.LOOKBACK_DAYS)).timestamp()))
        found = {}
        try:
            posts = self.paged(f'/{self.user_id}/threads', {
                'fields': FIELDS, 'since': since, 'limit': '25',
            }, pages=3)
        except MissingScope as error:
            self.say(f'token 沒有 {error.scope}，這輪不讀留言', 'notice')
            return []
        for post in posts:
            if not self.within_lookback(post):
                continue
            if post.get('has_replies') is False:
                continue
            post_id = media_id_of(post)
            if not post_id:
                continue
            try:
                self.absorb(post_id, post.get('permalink') or '', found, post.get('text') or '')
            except MissingScope as error:
                self.say(f'token 沒有 {error.scope}，這輪不讀留言串', 'notice')
                return []
        try:
            own_replies = self.paged(f'/{self.user_id}/replies', {
                'fields': FIELDS, 'since': since, 'limit': '25',
            })
        except MissingScope as error:
            self.say(f'token 沒有 {error.scope}，讀不到我們在別處的留言', 'notice')
            own_replies = []
        for reply in own_replies:
            if not self.within_lookback(reply) or reply.get('has_replies') is False:
                continue
            reply_id = media_id_of(reply)
            if not reply_id:
                continue
            try:
                self.absorb(reply_id, reply.get('permalink') or '', found, reply.get('text') or '')
            except MissingScope as error:
                self.say(f'token 沒有 {error.scope}，讀不到我們留言底下的回覆', 'notice')
                break
        if self.scopes is not None and C.MENTION_SCOPE not in self.scopes:
            self.say('token 沒有 threads_manage_mentions，這輪不讀提及', 'notice')
        else:
            try:
                mentions = self.paged(f'/{self.user_id}/mentions', {
                    'fields': FIELDS, 'since': since, 'limit': '25',
                })
            except MissingScope:
                self.say('token 沒有 threads_manage_mentions，這輪不讀提及', 'notice')
                mentions = []
            for mention in mentions:
                source_id = media_id_of(mention)
                if not source_id or source_id in found or is_ours(mention, self.username):
                    continue
                if not self.within_lookback(mention):
                    continue
                if mention.get('has_replies') is True:
                    try:
                        self.absorb(source_id, mention.get('permalink') or '', found, mention.get('text') or '')
                    except MissingScope as error:
                        self.say(f'token 沒有 {error.scope}，這輪不讀提及串', 'notice')
                        break
                if source_id in found:
                    continue
                when = C.parse_iso(mention.get('timestamp'))
                found[source_id] = {
                    'source_id': source_id,
                    'username': username_of(mention),
                    'text': mention.get('text') or '',
                    'permalink': mention.get('permalink') or '',
                    'root_url': mention.get('permalink') or '',
                    'hide_status': mention.get('hide_status'),
                    'ts': when.isoformat() if when else '',
                }
        ordered = []
        for item in found.values():
            if not item.get('username'):
                continue
            if C.is_hidden(item.get('hide_status')):
                if item['source_id'] not in self.handled():
                    self.discard('reply', item, 'hidden')
                continue
            ordered.append(item)
        ordered.sort(key=lambda item: (item.get('ts') or '', item['source_id']))
        return ordered

    def already_on_post(self, post_id):
        try:
            items = self.paged(f'/{post_id}/conversation', {'fields': 'id,username,is_reply_owned_by_me', 'limit': '50'}, pages=1)
        except MissingScope:
            return False
        except ApiError:
            return False
        return any(is_ours(item, self.username) for item in items)

    def collect_keywords(self):
        if not self.discovery_on:
            self.say('關鍵字留言這輪關閉')
            return
        if not self.llm_ready:
            self.say('Gemini 不可用，這輪不在別人的貼文留言（固定句型留在陌生人貼文太像洗版）', 'notice')
            return
        for keyword in C.keyword_order(self.now()):
            if self.comment_count >= C.COMMENT_RUN_CAP:
                self.say(f'本輪留言已達 {C.COMMENT_RUN_CAP} 則')
                return
            try:
                found = self.paged('/keyword_search', {
                    'q': keyword,
                    'search_type': 'TOP',
                    'fields': SEARCH_FIELDS,
                    'limit': '10',
                }, pages=1)
            except MissingScope:
                self.say('token 沒有 threads_keyword_search，這輪不搜尋公開貼文', 'notice')
                self.discovery_on = False
                return
            self.say(f'關鍵字「{keyword}」讀到 {len(found)} 則')
            for post in found:
                if self.comment_count >= C.COMMENT_RUN_CAP:
                    self.say(f'本輪留言已達 {C.COMMENT_RUN_CAP} 則')
                    return
                source_id = media_id_of(post)
                if not source_id:
                    continue
                normalized = {
                    'username': username_of(post),
                    'is_reply': as_bool(post.get('is_reply')),
                    'media_type': post.get('media_type'),
                    'text': post.get('text') or '',
                    'timestamp': C.parse_iso(post.get('timestamp')),
                }
                reason = C.discovery_skip_reason(normalized, self.now())
                if reason:
                    self.tally(reason)
                    continue
                block = C.comment_block_reason(
                    self.history + self.rows,
                    day=self.now().date().isoformat(),
                    author=normalized['username'],
                    source_id=source_id,
                    run_count=self.comment_count,
                )
                if block in ('run_cap', 'day_cap'):
                    self.say(f"關鍵字留言停在{C.REASON_LABEL.get(block, block)}")
                    return
                if block:
                    self.tally(block)
                    continue
                if post.get('has_replies') is True and self.already_on_post(source_id):
                    self.tally('same_post')
                    continue
                item = {
                    'source_id': source_id,
                    'username': normalized['username'],
                    'text': normalized['text'],
                    'permalink': post.get('permalink') or '',
                    'root_url': post.get('permalink') or '',
                }
                text, problems = self.compose('comment', item['text'], source_id, None)
                if text is None:
                    self.tally('guardrail')
                    self.say(f"留言文案未過，略過 {item['permalink'] or source_id}：{'、'.join(problems)}", 'warning')
                    continue
                self.comment_count += 1
                self.send('comment', item, text, 'genuine', False)

    def quota_allows_send(self):
        try:
            payload = self.call('GET', f'/{self.user_id}/threads_publishing_limit', {
                'fields': 'quota_usage,config,reply_quota_usage,reply_config',
            })
        except MissingScope:
            return True
        except ApiError as error:
            self.say(f'讀不到發文額度，仍照原計畫：{self.scrub(str(error))[:160]}', 'warning')
            return True
        row = ((payload.get('data') or [{}])[0]) if isinstance(payload, dict) else {}
        usage = row.get('quota_usage')
        total = (row.get('config') or {}).get('quota_total')
        replies = row.get('reply_quota_usage')
        reply_total = (row.get('reply_config') or {}).get('quota_total')
        self.say(f'24 小時額度：貼文 {usage}/{total}，回覆 {replies}/{reply_total}')
        if isinstance(replies, int) and isinstance(reply_total, int) and reply_total - replies <= 10:
            self.say('回覆額度剩下不到 10，這輪不送出', 'error')
            return False
        if isinstance(usage, int) and usage >= 240:
            self.say('貼文額度已達 240，這輪不送出', 'error')
            return False
        return True

    def decide_mode(self, dry_run):
        enabled = (self.env.get('THREADS_ENGAGE_ENABLED') or '').strip().lower() == 'true'
        if not dry_run and not enabled:
            dry_run = True
            self.say('THREADS_ENGAGE_ENABLED 未打開，這次只模擬，不送出任何留言', 'notice')
        if not dry_run and self.branch != 'main':
            raise Abort(f'只能從 main 送出互動，這次是 {self.branch}')
        self.dry_run = dry_run
        flag = (self.env.get('THREADS_ENGAGE_DISCOVERY') or '').strip().lower()
        if flag == 'false':
            self.discovery_on = False
        elif flag == 'true':
            self.discovery_on = True
        elif self.scopes is not None and C.SEARCH_SCOPE in self.scopes:
            self.discovery_on = True
        else:
            self.discovery_on = False

    def preflight(self):
        if not self.token:
            raise Abort('HIBOU_THREADS_TOKEN 沒有設定')
        expected = (self.env.get('HIBOU_THREADS_USERNAME') or '').strip().lstrip('@')
        if not expected:
            raise Abort('HIBOU_THREADS_USERNAME 沒有設定')
        me = self.call('GET', '/me', {'fields': 'id,username'})
        self.username = me.get('username') or ''
        self.user_id = str(me.get('id') or '')
        if self.username != expected:
            raise Abort(f'帳號不符：預期 @{expected}，實際 @{self.username}；這輪不送出')
        expected_id = (self.env.get('HIBOU_THREADS_USER_ID') or '').strip()
        if expected_id and self.user_id != expected_id:
            raise Abort(f'帳號不符：預期 id {expected_id}，實際 {self.user_id}；這輪不送出')
        if not MEDIA_ID_RE.fullmatch(self.user_id):
            raise Abort('Threads 沒有回傳可用的帳號 id')
        self.say(f'帳號：@{self.username}（id {self.user_id}）')
        self.scopes = self.fetch_scopes()
        if self.scopes is None:
            self.say('讀不到 token 權限清單，改由實際呼叫結果判斷', 'notice')
        else:
            self.say('token 權限：' + (', '.join(sorted(self.scopes)) or '（空）'))
            missing = [scope for scope in C.PHASE_A_SCOPES if scope not in self.scopes]
            if missing:
                self.preview_only = True
                self.say('缺少 ' + '、'.join(missing) + '，這輪只模擬回覆，不送出', 'notice')
        if self.provider_fallback:
            self.say('指定了 Gemini 但沒有金鑰，改用固定句型', 'warning')
        elif self.provider == 'gemini':
            self.say(f'文案：Gemini（{self.model}）')
        else:
            self.say('文案：固定句型（未設定 GEMINI_API_KEY）')

    def finish_counts(self):
        def count(action, kind_type=None):
            return sum(1 for row in self.rows if row.get('action') == action and (kind_type is None or row.get('type') == kind_type))
        sent = '模擬' if self.dry_run or self.preview_only else '送出'
        self.say(
            f"{sent}回覆 {count('posted', 'reply') + count('dry_run', 'reply')} 則、"
            f"留言 {count('posted', 'comment') + count('dry_run', 'comment')} 則、"
            f"略過 {count('skipped') + count('dry_run_skip')} 則"
        )
        stopped = count('stopped')
        if stopped:
            self.say(f'中止記錄 {stopped} 則')
        if self.skip_tallies:
            detail = '，'.join(f"{C.REASON_LABEL.get(key, key)} {value}" for key, value in sorted(self.skip_tallies.items()))
            self.say(f'關鍵字略過：{detail}')
        humans = [row for row in self.rows if row.get('needs_human')]
        if humans:
            self.say(f'需要人工跟進 {len(humans)} 則（見上方警告）', 'warning')

    def run(self, dry_run=False):
        mask_secret(self.token, self.env)
        mask_secret(self.gemini_key, self.env)
        self.preflight()
        self.decide_mode(dry_run)
        self.say('模式：只模擬' if self.dry_run or self.preview_only else '模式：正式送出')
        if not self.dry_run and not self.preview_only and not self.quota_allows_send():
            self.failed = True
            self.finish_counts()
            return 1
        self.load_history()
        try:
            for item in self.collect_replies():
                if self.reply_count >= C.REPLY_RUN_CAP:
                    self.say(f'本輪回覆已達 {C.REPLY_RUN_CAP} 則，其餘留給下一輪')
                    break
                self.consider_reply(item)
            self.collect_keywords()
        except StopRun as error:
            self.failed = True
            self.say(f'立即停止：{error}', 'error')
            self.say('請 Otto 先看帳號有沒有被限制。要停機就把 THREADS_ENGAGE_ENABLED 改成 false', 'warning')
        except MissingScope as error:
            self.say(f'權限不足（{error.scope}），這輪沒有繼續送出', 'notice')
        self.finish_counts()
        return 1 if self.failed else 0


def main(argv=None, **runner_args):
    parser = argparse.ArgumentParser(description='Reply and comment for @lovehibou.')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    runner = Runner(**runner_args)
    code = 1
    try:
        code = runner.run(dry_run=args.dry_run)
    except Abort as error:
        runner.say(runner.scrub(str(error)), 'error')
        code = 1
    except ApiError as error:
        runner.say(runner.scrub(str(error)), 'error')
        code = 1
    finally:
        runner.write_preview()
        runner.write_summary()
    return code


if __name__ == '__main__':
    sys.exit(main())
