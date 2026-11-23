"""Guardrail, dedupe, pacing and dry-run tests for Threads engagement.

Nothing here touches the network or git. The Threads API is a fake.
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'social'))
import engage as E  # noqa: E402
import engage_copy as C  # noqa: E402

TAIPEI = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 23, 9, 0, tzinfo=TAIPEI)
USER_ID = '1234567890'
TOKEN = 'fixture-token-do-not-print'
GEMINI = 'fixture-gemini-key-do-not-print'
SCOPES = [
    'threads_basic', 'threads_content_publish', 'threads_read_replies',
    'threads_manage_replies', 'threads_manage_mentions',
]
ENV = {
    'HIBOU_THREADS_TOKEN': TOKEN,
    'HIBOU_THREADS_USERNAME': 'lovehibou',
    'HIBOU_THREADS_USER_ID': USER_ID,
    'THREADS_ENGAGE_ENABLED': 'true',
}


def permission_error(scope):
    return E.ApiError('GET', '/x', 403, json.dumps({
        'error': {'message': f'(#10) Requires {scope} permission', 'code': 10, 'type': 'OAuthException'},
    }))


def rate_error():
    return E.ApiError('POST', '/threads', 429, json.dumps({
        'error': {'message': 'Application request limit reached', 'code': 4},
    }))


class FakeThreads:
    def __init__(self, scopes=None, username='lovehibou', user_id=USER_ID):
        self.scopes = list(SCOPES if scopes is None else scopes)
        self.username = username
        self.user_id = user_id
        self.threads = []
        self.own_replies = []
        self.mentions = []
        self.conversations = {}
        self.search = {}
        self.fail = {}
        self.fail_create_after = None
        self.creates = 0
        self.calls = []
        self.quota = {
            'quota_usage': 3,
            'config': {'quota_total': 250},
            'reply_quota_usage': 4,
            'reply_config': {'quota_total': 1000},
        }

    def __call__(self, method, path, params, token):
        self.calls.append((method, path, dict(params), token))
        for suffix, error in self.fail.items():
            if path == suffix or path.endswith(suffix):
                raise error
        if path == '/me':
            return {'id': self.user_id, 'username': self.username}
        if path == '/debug_token':
            return {'data': {'is_valid': True, 'scopes': list(self.scopes)}}
        if path.endswith('/threads_publishing_limit'):
            return {'data': [self.quota]}
        if method == 'GET' and path == f'/{self.user_id}/threads':
            return {'data': self.threads}
        if method == 'GET' and path == f'/{self.user_id}/replies':
            return {'data': self.own_replies}
        if method == 'GET' and path == f'/{self.user_id}/mentions':
            return {'data': self.mentions}
        if method == 'GET' and path.endswith('/conversation'):
            media_id = path.strip('/').split('/')[0]
            return {'data': self.conversations.get(media_id, [])}
        if path == '/keyword_search':
            return {'data': self.search.get(params.get('q'), [])}
        if method == 'POST' and path.endswith('/threads_publish'):
            return {'id': 'media-' + params['creation_id']}
        if method == 'POST' and path.endswith('/threads'):
            self.creates += 1
            if self.fail_create_after is not None and self.creates > self.fail_create_after:
                raise rate_error()
            return {'id': f'container{self.creates}'}
        if method == 'GET' and path.startswith('/container'):
            return {'status': 'FINISHED'}
        if method == 'GET' and path.startswith('/media-'):
            return {'permalink': 'https://www.threads.com/@lovehibou/post/' + path[1:]}
        raise AssertionError(f'unexpected {method} {path}')


def reply(media_id, username, text, hour, **extra):
    item = {
        'id': media_id,
        'username': username,
        'text': text,
        'timestamp': f'2026-11-22T{hour:02d}:00:00+0000',
        'permalink': f'https://www.threads.com/@{username}/post/{media_id}',
        'is_reply': True,
        'is_reply_owned_by_me': False,
        'hide_status': 'NOT_HUSHED',
    }
    item.update(extra)
    return item


class CopyTests(unittest.TestCase):
    def test_guardrail_rejects_banned_drafts(self):
        samples = [
            '看 https://www.ihibou.com/a 這個好可愛',
            '看 www.ihibou.com 這個好可愛',
            '這件 NT$1280 好好用',
            '這件 $12 好好用',
            '這件 350元 好好用',
            '日本製的紗布好軟',
            '日本設計的紗布好軟',
            '日本熱銷的紗布好軟',
            '還有現貨，私訊我們',
            '交期再跟你說，私訊我們',
            '庫存的事私訊我們就好',
            '有 UPF 的那款，私訊我們',
            '抗菌這件事私訊我們',
            '保證好看，拍拍你',
            '感謝您的分享，拍拍你。',
            '非常理解您的感受，拍拍。',
            '希望對您有幫助，拍拍。',
            '每個寶寶都是獨一無二的，拍拍。',
            '您好，拍拍你。',
            '#育兒日常 好好笑，拍拍。',
            '#帶娃出門崩潰日記 好真實，拍拍。',
            '這是 AI 製作的圖，拍拍。',
            '以 AI 畫的，拍拍你。',
            '1. 先拍拍\n2. 再休息',
            '甲。乙。丙。丁。',
            '宝宝好可爱，拍拍。',
            '沒錯！報完戶口記得順便把津貼申請送出去，60 天內辦好才能補領到 12 月喔。',
            '報完戶口記得，60 天內辦好喔。',
            '育兒津貼有一個 60 天關卡。',
            '我們家的呼呼大睡被很軟，快來買。',
            '   ',
            '',
            '三個月還在夜醒' + '🦉' * 3,
            '中' * 91,
        ]
        for sample in samples:
            self.assertTrue(C.validate_draft(sample), sample)

    def test_guardrail_accepts_colloquial_lines_and_fixed_copy(self):
        accepted = [
            C.GIFT_DATE_TEXT,
            '三個月還在夜醒，拍拍你。',
            '看到三個月，新手時期真的會這樣。',
            '三個月還在夜醒，拍拍你 🦉',
            '三個月還在夜醒 🦉🌙',
            '甲。乙。丙。',
            *C.COMMERCE_TEXT.values(),
            *C.SENSITIVE_LINES,
            *C.OFFICIAL_CHECK_LINES,
            C.SAFE_FALLBACK,
        ]
        for sample in accepted:
            self.assertEqual(C.validate_draft(sample), [], sample)

    def test_fixed_lines_do_not_leak_price_stock_or_urls(self):
        for text in (C.GIFT_DATE_TEXT, *C.COMMERCE_TEXT.values(), *C.SENSITIVE_LINES):
            self.assertNotIn('您', text)
            self.assertFalse(C.URL_RE.search(text))
            self.assertFalse(C.PRICE_RE.search(text))
            for word in ('現貨', '出貨', '交期', '日本', 'http', '#'):
                self.assertNotIn(word, text)

    def test_gift_date_text_is_exact(self):
        self.assertEqual(C.fixed_text('gift_date', '19003'), '有送禮日的話，先用 LINE 問我們')

    def test_classify_inbound(self):
        cases = {
            '寶寶三個月還在夜醒，我快不行了': 'genuine',
            '尺寸剛好，謝謝': 'genuine',
            '尺寸呢': 'commerce',
            '多少錢': 'commerce',
            '可以刺繡名字嗎': 'commerce',
            '預產期下週，趕得及嗎': 'gift_date',
            '用了之後寶寶過敏': 'sensitive',
            '加賴投資群組，日賺一萬': 'spam',
            '流產之後不知道怎麼辦': 'grief',
            '這次選舉真的好累': 'skip_topic',
            '': 'empty',
        }
        for text, kind in cases.items():
            self.assertEqual(C.classify_inbound(text)['class'], kind, text)
        self.assertEqual(C.commerce_subtype('可以刺繡名字嗎'), 'embroidery')
        self.assertEqual(C.commerce_subtype('尺寸呢'), 'size')

    def test_detail_drops_japan_claim_and_templates_vary(self):
        detail = C.concrete_detail('日本製的紗布用了三個月，軟軟的')
        self.assertIn('三個月', detail)
        self.assertNotIn('日本', detail)
        texts = [C.template_text('genuine', '寶寶三個月還在夜醒', str(i)) for i in range(8)]
        self.assertGreaterEqual(len(set(texts)), 3)
        for text in texts:
            self.assertEqual(C.validate_draft(text), [], text)
            self.assertIn('三個月', text)

    def test_poll_number_answers_pick_the_option(self):
        post = ('11、12 月出生的寶寶，育兒津貼有一個 60 天關卡。\n'
                '留言報數字：你家寶寶 1 十一月出生 2 十二月出生 3 還在肚子裡 4 已經會跑了')
        for answer, option in (('2', '十二月出生'), ('我選3！', '還在肚子裡'), ('４', '已經會跑了')):
            for sid in ('a', 'b', 'c'):
                text = C.template_text('genuine', answer, sid, None, post)
                self.assertIn(option, text)
                self.assertEqual(C.validate_draft(text), [], text)
        battery = '小鷹家電量報告。\n留言報數字：你現在剩幾 %？'
        self.assertIn(C.template_text('genuine', '剩8%', 'a', None, battery), C.POLL_BARE_LINES)
        self.assertNotIn(C.template_text('genuine', '2', 'a', None, '沒有投票'), C.POLL_BARE_LINES)

    def test_long_comment_is_never_quoted_half(self):
        source = '8% 對 100% 這個差距太真實😂\n小鷹假裝成抱枕最聰明 至少還能充一點電'
        self.assertEqual(C.concrete_detail(source), '這段')
        for sid in ('a', 'b', 'c', 'd'):
            text = C.template_text('genuine', source, sid)
            self.assertNotIn('「', text)
            self.assertEqual(C.validate_draft(text), [], text)
        self.assertEqual(C.concrete_detail('寶寶 3 個月還在夜醒，我快不行了'), '寶寶3個月還在夜醒')
        self.assertIn(C.template_text('genuine', '哈哈哈哈', 'a'), C.LAUGH_LINES)

    def test_prompt_carries_the_post_being_answered(self):
        prompt = C.build_prompt('genuine', '2', C.concrete_detail('2'), '留言報數字：1 十一月 2 十二月')
        self.assertIn('這則留言回覆的貼文：留言報數字', prompt)
        self.assertNotIn('「這段」', prompt)
        self.assertIn('不要講任何天數', prompt)
        self.assertIn('戶政', prompt)
        comment = C.build_prompt('comment', '夜奶好累眼睛睜不開', '夜奶好累眼睛睜不開')
        self.assertIn('輕的問句', comment)
        self.assertIn('不要推銷', comment)

    def test_dedupe_ignores_dry_run_and_stopped(self):
        rows = [
            {'source_id': '1', 'action': 'posted', 'dry_run': False},
            {'source_id': '2', 'action': 'dry_run', 'dry_run': True},
            {'source_id': '3', 'action': 'skipped', 'dry_run': False},
            {'source_id': '4', 'action': 'stopped', 'dry_run': False},
        ]
        self.assertEqual(C.handled_source_ids(rows), {'1', '3'})

    def test_load_log_skips_broken_lines(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / '2026-11-23.jsonl'
            path.write_text('{"source_id": "9", "action": "posted"}\nnot-json\n\n', encoding='utf-8')
            rows = C.load_log_rows([path])
        self.assertEqual(C.handled_source_ids(rows), {'9'})

    def test_comment_caps(self):
        day = '2026-11-23'
        rows = []
        for index in range(40):
            rows.append({
                'ts': f'{day}T09:00:00+08:00', 'type': 'comment', 'action': 'posted',
                'dry_run': False, 'source_id': str(50000 + index), 'author_key': f'k{index % 30}',
            })
        self.assertEqual(C.comment_block_reason(
            rows, day=day, author='knewbie', source_id='60001', run_count=0,
        ), 'day_cap')
        rows = [
            {'ts': f'{day}T09:00:00+08:00', 'type': 'comment', 'action': 'posted',
             'source_id': '60001', 'author_key': 'kmaya', 'dry_run': False},
            {'ts': f'{day}T10:00:00+08:00', 'type': 'comment', 'action': 'posted',
             'source_id': '60002', 'author_key': 'kmaya', 'dry_run': False},
        ]
        self.assertEqual(C.comment_block_reason(
            rows, day=day, author='kmaya', source_id='60003', run_count=0,
        ), 'author_cap')
        self.assertEqual(C.comment_block_reason(
            rows, day=day, author='kother', source_id='60001', run_count=0,
        ), 'same_post')
        # rows written before author_key existed never match, and an empty key never matches
        old = [dict(row, author_key='') for row in rows]
        self.assertIsNone(C.comment_block_reason(old, day=day, author='', source_id='60003', run_count=0))
        self.assertEqual(C.comment_block_reason(
            [], day=day, author='other', source_id='60009', run_count=8,
        ), 'run_cap')
        self.assertIsNone(C.comment_block_reason(
            [], day=day, author='other', source_id='60009', run_count=7,
        ))

    def test_quiet_windows_and_gap(self):
        def at(hour, minute, second=0):
            return datetime(2026, 11, 23, hour, minute, second, tzinfo=TAIPEI)

        self.assertIsNone(C.quiet_resume_at(at(12, 24, 59)))
        self.assertEqual(C.quiet_resume_at(at(12, 25)).strftime('%H:%M'), '12:35')
        self.assertEqual(C.quiet_resume_at(at(12, 34, 59)).strftime('%H:%M'), '12:35')
        self.assertIsNone(C.quiet_resume_at(at(12, 35)))
        self.assertEqual(C.quiet_resume_at(at(7, 30)).strftime('%H:%M'), '07:35')
        self.assertEqual(C.quiet_resume_at(at(21, 29)).strftime('%H:%M'), '21:35')
        self.assertIsNone(C.quiet_resume_at(at(9, 0)))
        self.assertIsNone(C.quiet_resume_at(at(18, 35)))
        now = at(9, 0)
        self.assertEqual(C.gap_seconds(None, now), 0)
        self.assertEqual(C.gap_seconds(now - timedelta(seconds=30), now), 30)
        self.assertEqual(C.gap_seconds(now - timedelta(seconds=61), now), 0)

    def test_latest_outbound_includes_scheduled_posts(self):
        rows = [
            {'ts': '2026-11-23T08:00:00+08:00', 'action': 'post', 'dry_run': False},
            {'ts': '2026-11-23T08:30:00+08:00', 'action': 'posted', 'dry_run': True},
            {'ts': '2026-11-23T07:00:00+08:00', 'action': 'self_reply', 'dry_run': False},
        ]
        self.assertEqual(C.latest_outbound_at(rows).isoformat(), '2026-11-23T08:00:00+08:00')

    def test_discovery_skips(self):
        def post(**changes):
            base = {
                'username': 'parent',
                'is_reply': False,
                'media_type': 'TEXT',
                'text': '懷孕二十週開始睡不好，每天只睡三小時',
                'timestamp': NOW - timedelta(days=1),
            }
            base.update(changes)
            return base

        self.assertIsNone(C.discovery_skip_reason(post(), NOW))
        self.assertEqual(C.discovery_skip_reason(post(media_type='IMAGE'), NOW), 'minor_photo')
        self.assertEqual(C.discovery_skip_reason(post(text='看看兒子照片，笑得好甜呀'), NOW), 'minor_photo')
        self.assertEqual(C.discovery_skip_reason(post(text='寶寶發燒了要不要看醫生，好擔心'), NOW), 'medical')
        self.assertEqual(C.discovery_skip_reason(post(text='流產之後不知道怎麼面對今天'), NOW), 'grief')
        self.assertEqual(C.discovery_skip_reason(post(text='這次選舉真的讓新手爸媽很累'), NOW), 'politics')
        self.assertEqual(C.discovery_skip_reason(post(text='留言抽寶寶用品免費送大家'), NOW), 'giveaway')
        self.assertEqual(C.discovery_skip_reason(post(text='加賴投資群組日賺一萬快來'), NOW), 'scam')
        self.assertEqual(C.discovery_skip_reason(post(username='lovehibou'), NOW), 'own_account')
        self.assertEqual(C.discovery_skip_reason(post(is_reply=True), NOW), 'is_reply')
        self.assertEqual(C.discovery_skip_reason(post(text='好可愛'), NOW), 'too_short')
        self.assertEqual(C.discovery_skip_reason(post(text='hello parents this is long'), NOW), 'not_chinese')
        self.assertEqual(C.discovery_skip_reason(post(timestamp=NOW - timedelta(days=20)), NOW), 'too_old')
        queries = C.discovery_queries(NOW)
        self.assertEqual(C.keyword_order(NOW), tuple(item['keyword'] for item in queries))
        self.assertEqual(len(queries), C.THEMES_PER_RUN * C.KEYWORDS_PER_THEME)
        labels = tuple(dict.fromkeys(item['label'] for item in queries))
        self.assertEqual(len(labels), C.THEMES_PER_RUN)
        pairs = []
        for hour in (9, 11, 14, 16, 19, 22):
            slot = C.discovery_queries(NOW.replace(hour=hour))
            pairs.append(tuple(dict.fromkeys(item['label'] for item in slot)))
        self.assertEqual(len(set(pairs)), 6)
        self.assertEqual(queries, C.discovery_queries(NOW))
        params = C.keyword_search_params(queries[0]['keyword'], NOW)
        self.assertEqual(params['search_type'], 'RECENT')
        self.assertEqual(params['media_type'], 'TEXT')
        self.assertEqual(params['since'], str(int((NOW - timedelta(days=C.LOOKBACK_DAYS)).timestamp())))

    def test_regulated_topic_points_at_the_official_source(self):
        self.assertIn(C.template_text('genuine', '津貼是不是 60 天內要申請', 'a'), C.OFFICIAL_CHECK_LINES)
        self.assertNotIn('60', C.template_text('genuine', '補領要幾天', 'b'))
        post = ('11、12 月出生的寶寶，育兒津貼有一個 60 天關卡。\n'
                '留言報數字：你家寶寶 1 十一月出生 2 十二月出生')
        self.assertIn('十二月出生', C.template_text('genuine', '2', 'c', None, post))

    def test_discovery_fit_and_rank(self):
        def post(text, **changes):
            base = {
                'username': 'parent',
                'is_reply': False,
                'media_type': 'TEXT',
                'text': text,
                'timestamp': NOW - timedelta(hours=5),
                'has_replies': False,
            }
            base.update(changes)
            return base

        self.assertIsNone(C.discovery_fit_reason(post('夜奶好累眼睛睜不開'), NOW, ('夜奶',)))
        self.assertEqual(C.discovery_fit_reason(post('夜奶代購這陣子好多'), NOW, ('夜奶',)), 'commercial')
        self.assertEqual(C.discovery_fit_reason(post('今天股市好高快來討論'), NOW, ('夜奶',)), 'off_topic')
        self.assertEqual(C.discovery_fit_reason(post('惡婆婆又在念夜奶的事好煩'), NOW, ('夜奶',)), 'complaint')
        live = post('夜奶好累眼睛睜不開，你們呢？', has_replies=True, timestamp=NOW - timedelta(hours=1))
        stale = post('夜奶好累眼睛睜不開', has_replies=False, timestamp=NOW - timedelta(days=4))
        self.assertGreater(C.engagement_rank(live, NOW), C.engagement_rank(stale, NOW))
        self.assertTrue(C.near_duplicate('夜奶好累，你們後來怎麼辦？', ['夜奶好累，你們後來怎麼辦？']))
        self.assertFalse(C.near_duplicate('推車風好大，你們怎麼擋？', ['夜奶好累，你們後來怎麼辦？']))

    def test_stop_and_permission_classification(self):
        hidden = E.ApiError('POST', '/threads', 400, json.dumps({
            'error': {'message': 'This reply is hidden', 'code': 100},
        }))
        policy = E.ApiError('POST', '/threads', 400, json.dumps({
            'error': {'message': 'Action blocked for community standards', 'code': 368},
        }))
        spam = E.ApiError('POST', '/threads', 400, json.dumps({
            'error': {'message': 'spam detected on this comment', 'code': 100},
        }))
        self.assertEqual(E.stop_reason(rate_error()), 'rate_limit')
        self.assertEqual(E.stop_reason(hidden), 'hidden')
        self.assertEqual(E.stop_reason(policy), 'policy')
        self.assertEqual(E.stop_reason(spam), 'spam')
        self.assertEqual(E.permission_scope(permission_error('threads_keyword_search')), 'threads_keyword_search')
        leaked = 'access_' + 'token=' + TOKEN
        self.assertNotIn(TOKEN, str(E.ApiError('GET', '/me', 400, leaked)))

    def test_mask_follows_the_run_env_not_the_runner(self):
        out = io.StringIO()
        with redirect_stdout(out):
            E.mask_secret(TOKEN, {'GITHUB_ACTIONS': 'true'})
            E.mask_secret('', {'GITHUB_ACTIONS': 'true'})
            E.mask_secret(TOKEN, {})
        self.assertEqual(out.getvalue(), f'::add-mask::{TOKEN}\n')


class EngageRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'social').mkdir()
        self.stdout = io.StringIO()

    def tearDown(self):
        self.tmp.cleanup()

    def fixture(self, api):
        api.threads = [{
            'id': '18001',
            'text': '今晚推車終於安靜',
            'timestamp': '2026-11-20T13:00:00+0000',
            'permalink': 'https://www.threads.com/@lovehibou/post/18001',
            'username': 'lovehibou',
            'has_replies': True,
        }]
        api.conversations['18001'] = [
            reply('19001', 'mama_lin', '寶寶三個月還在夜醒，我快不行了', 1),
            reply('19002', 'spammer', '加賴投資群組，日賺一萬', 2),
            reply('19003', 'gift_parent', '預產期下週，趕得及嗎', 3),
            reply('19004', 'worry_parent', '用了之後寶寶過敏，紅紅的', 4),
            reply('19005', 'lovehibou', '我們也是這樣', 5),
            reply('19006', 'tired_parent', '也好想睡', 6),
            reply('19007', 'lovehibou', '拍拍', 7, is_reply_owned_by_me=True, replied_to={'id': '19006'}),
        ]
        api.own_replies = [{
            'id': '20001',
            'text': '這段好熟悉',
            'timestamp': '2026-11-21T02:00:00+0000',
            'permalink': 'https://www.threads.com/@lovehibou/post/20001',
            'username': 'lovehibou',
            'has_replies': True,
        }]
        api.conversations['20001'] = [
            reply('20002', 'dad_chen', '你們的紗布巾尺寸呢', 8),
        ]

    def execute(self, api, *args, env=None, now=None, complete=None):
        sleeps = []
        with redirect_stdout(self.stdout):
            code = E.main(
                list(args), root=self.root, env=dict(ENV if env is None else env), api=api,
                clock=lambda: now or NOW, sleep=lambda seconds: sleeps.append(seconds),
                commit=False, complete=complete,
            )
        return code, sleeps

    def public_post(self, media_id, username, text, timestamp, replies=False, media_type='TEXT'):
        return {
            'id': media_id,
            'text': text,
            'media_type': media_type,
            'permalink': f'https://www.threads.com/@{username}/post/{media_id}',
            'timestamp': timestamp,
            'username': username,
            'has_replies': replies,
            'is_reply': False,
        }

    def posted_texts(self, api):
        return [params.get('text') for method, path, params, _token in api.calls
                if method == 'POST' and path.endswith('/threads') and not path.endswith('/threads_publish')]

    def posted_targets(self, api):
        return [params.get('reply_to_id') for method, path, params, _token in api.calls
                if method == 'POST' and path.endswith('/threads') and not path.endswith('/threads_publish')]

    def log_rows(self):
        folder = self.root / 'social' / 'engagement'
        rows = []
        if not folder.is_dir():
            return rows
        for path in sorted(folder.glob('*.jsonl')):
            rows.extend(json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip())
        return rows

    def test_live_replies_log_and_second_run_dedupes(self):
        api = FakeThreads()
        self.fixture(api)
        code, sleeps = self.execute(api)
        self.assertEqual(code, 0, self.stdout.getvalue())
        targets = self.posted_targets(api)
        self.assertEqual(targets, ['19001', '19003', '19004', '20002'])
        texts = self.posted_texts(api)
        self.assertIn('三個月', texts[0])
        self.assertEqual(texts[1], '有送禮日的話，先用 LINE 問我們')
        self.assertIn(texts[2], C.SENSITIVE_LINES)
        self.assertEqual(texts[3], C.COMMERCE_TEXT['size'])
        for text in texts:
            self.assertEqual(C.validate_draft(text), [], text)
        self.assertNotIn('19002', targets)
        self.assertNotIn('19006', targets)
        self.assertTrue(any(seconds >= 60 for seconds in sleeps))
        output = self.stdout.getvalue()
        self.assertIn('需要人工跟進 @w***', output)
        self.assertNotIn(TOKEN, output)
        rows = self.log_rows()
        posted = [row for row in rows if row['action'] == 'posted']
        self.assertEqual([row['source_id'] for row in posted], targets)
        for row in posted:
            self.assertEqual(row['type'], 'reply')
            self.assertEqual(len(row['author_key']), 16)
            self.assertTrue(row['text'])
            self.assertTrue(row['media_id'])
            self.assertTrue(row['permalink'].startswith('https://www.threads.com/@lovehibou/'))
        self.assertTrue(any(row['source_id'] == '19002' and row['action'] == 'skipped' for row in rows))
        # The log is committed to a public repo: no other account names, their links or content labels.
        dumped = json.dumps(rows, ensure_ascii=False) + output
        for name in ('mama_lin', 'spammer', 'gift_parent', 'worry_parent', 'dad_chen'):
            self.assertNotIn(name, dumped)
        for row in rows:
            for field in ('other_account', 'source_url', 'root_url', 'needs_human'):
                self.assertNotIn(field, row)
            self.assertIsNone(row['reason'])
        self.assertIn('需要人工跟進 @w***（需人工）https://www.threads.com/@lovehibou/', output)
        for _method, path, params, token in api.calls:
            self.assertEqual(token, TOKEN)
            if path != '/debug_token':
                self.assertNotIn(TOKEN, json.dumps(params))

        again = FakeThreads()
        self.fixture(again)
        self.stdout = io.StringIO()
        code, _sleeps = self.execute(again)
        self.assertEqual(code, 0, self.stdout.getvalue())
        self.assertEqual(again.creates, 0)

    def test_dry_run_posts_nothing_and_switch_blocks_live(self):
        api = FakeThreads()
        self.fixture(api)
        code, _sleeps = self.execute(api, '--dry-run')
        self.assertEqual(code, 0, self.stdout.getvalue())
        self.assertEqual(api.creates, 0)
        output = self.stdout.getvalue()
        self.assertIn('有送禮日的話，先用 LINE 問我們', output)
        self.assertIn('需要人工跟進 @w***', output)
        self.assertEqual(self.log_rows(), [])
        self.assertNotIn(TOKEN, output)

        blocked = FakeThreads()
        self.fixture(blocked)
        self.stdout = io.StringIO()
        env = dict(ENV, THREADS_ENGAGE_ENABLED='false')
        code, _sleeps = self.execute(blocked, env=env)
        self.assertEqual(code, 0, self.stdout.getvalue())
        self.assertEqual(blocked.creates, 0)
        self.assertIn('只模擬', self.stdout.getvalue())

    def test_wrong_account_sends_nothing(self):
        api = FakeThreads(username='someoneelse')
        self.fixture(api)
        code, _sleeps = self.execute(api)
        self.assertEqual(code, 1)
        self.assertEqual(api.creates, 0)
        self.assertNotIn(TOKEN, self.stdout.getvalue())

    def test_missing_read_scope_is_clean(self):
        scopes = [scope for scope in SCOPES if scope != 'threads_read_replies']
        api = FakeThreads(scopes=scopes)
        self.fixture(api)
        code, _sleeps = self.execute(api)
        self.assertEqual(code, 0, self.stdout.getvalue())
        self.assertEqual(api.creates, 0)
        self.assertIn('threads_read_replies', self.stdout.getvalue())
        self.assertFalse(any(path.endswith('/conversation') for _m, path, _p, _t in api.calls))

    def test_missing_scope_on_the_call_is_clean(self):
        api = FakeThreads()
        api.fail['/debug_token'] = E.ApiError('GET', '/debug_token', 400, json.dumps({
            'error': {'message': 'unsupported', 'code': 100},
        }))
        api.fail['/conversation'] = permission_error('threads_read_replies')
        self.fixture(api)
        code, _sleeps = self.execute(api)
        self.assertEqual(code, 0, self.stdout.getvalue())
        self.assertEqual(api.creates, 0)
        self.assertNotIn(TOKEN, self.stdout.getvalue())

    def test_rate_limit_stops_before_later_replies(self):
        api = FakeThreads()
        self.fixture(api)
        api.fail_create_after = 1
        code, _sleeps = self.execute(api)
        self.assertEqual(code, 1)
        rows = self.log_rows()
        self.assertEqual([row['source_id'] for row in rows if row['action'] == 'posted'], ['19001'])
        self.assertTrue(any(row['action'] == 'stopped' and row['source_id'] == '19003' and row['reason'] == 'rate_limit' for row in rows))
        attempted = [params.get('reply_to_id') for method, path, params, _token in api.calls
                     if method == 'POST' and path.endswith('/threads')]
        self.assertEqual(attempted, ['19001', '19003'])
        self.assertIn('速率限制', self.stdout.getvalue())

    def test_quiet_window_waits(self):
        api = FakeThreads()
        api.threads = [{
            'id': '18001', 'timestamp': '2026-11-22T01:00:00+0000', 'permalink': 'https://www.threads.com/@lovehibou/post/18001',
            'username': 'lovehibou', 'has_replies': True,
        }]
        api.conversations['18001'] = [reply('19001', 'mama_lin', '寶寶三個月還在夜醒，我快不行了', 1)]
        when = datetime(2026, 11, 23, 12, 28, tzinfo=TAIPEI)
        code, sleeps = self.execute(api, now=when)
        self.assertEqual(code, 0, self.stdout.getvalue())
        self.assertGreaterEqual(sleeps[0], 400)
        self.assertIn('12:35', self.stdout.getvalue())

    def test_bad_model_draft_never_posts(self):
        api = FakeThreads()
        api.threads = [{
            'id': '18001', 'timestamp': '2026-11-22T01:00:00+0000', 'permalink': 'https://www.threads.com/@lovehibou/post/18001',
            'username': 'lovehibou', 'has_replies': True,
        }]
        api.conversations['18001'] = [reply('19001', 'mama_lin', '寶寶三個月還在夜醒，我快不行了', 1)]
        env = dict(ENV, GEMINI_API_KEY=GEMINI)

        def bad(_prompt):
            return '看這篇 https://evil.example/a 日本製 350元'

        code, _sleeps = self.execute(api, env=env, complete=bad)
        self.assertEqual(code, 0, self.stdout.getvalue())
        texts = self.posted_texts(api)
        self.assertEqual(len(texts), 1)
        self.assertNotIn('http', texts[0])
        self.assertNotIn('日本', texts[0])
        self.assertNotIn('元', texts[0])
        self.assertIn('三個月', texts[0])
        output = self.stdout.getvalue()
        self.assertNotIn(TOKEN, output)
        self.assertNotIn(GEMINI, output)

    def test_discovery_off_without_scope_and_on_with_caps(self):
        quiet = FakeThreads()
        code, _sleeps = self.execute(quiet)
        self.assertEqual(code, 0, self.stdout.getvalue())
        self.assertFalse(any(path == '/keyword_search' for _m, path, _p, _t in quiet.calls))

        scopes = SCOPES + ['threads_keyword_search']
        forced_off = FakeThreads(scopes=scopes)
        self.stdout = io.StringIO()
        env = dict(ENV, THREADS_ENGAGE_DISCOVERY='false')
        code, _sleeps = self.execute(forced_off, env=env)
        self.assertEqual(code, 0)
        self.assertFalse(any(path == '/keyword_search' for _m, path, _p, _t in forced_off.calls))

        api = FakeThreads(scopes=scopes)
        keyword = C.keyword_order(NOW)[0]
        posts = [
            self.public_post('31000', 'maya', '夜奶好累眼睛睜不開', '2026-11-22T01:00:00+0000', replies=False),
            self.public_post('31001', 'maya', '哄睡失敗寶寶又醒了', '2026-11-22T18:00:00+0000', replies=True),
            self.public_post('31002', 'maya', '推車出門風好大呀', '2026-11-23T00:30:00+0000', replies=True),
            self.public_post('31010', 'other', '夜奶好累眼睛睜不開', '2026-11-22T01:00:00+0000', media_type='IMAGE'),
            self.public_post('31011', 'other', '寶寶發燒了要不要看醫生，好擔心', '2026-11-22T01:00:00+0000'),
        ]
        api.search[keyword] = posts
        self.stdout = io.StringIO()
        code, _sleeps = self.execute(api)
        self.assertEqual(code, 0, self.stdout.getvalue())
        self.assertEqual(self.posted_targets(api), ['31002', '31001'])
        searches = [params for method, path, params, _token in api.calls if path == '/keyword_search']
        self.assertEqual(len(searches), len(C.keyword_order(NOW)))
        self.assertEqual(searches[0]['search_type'], 'RECENT')
        self.assertEqual(searches[0]['media_type'], 'TEXT')
        self.assertIn('Gemini 不可用', self.stdout.getvalue())
        self.assertIn('關鍵字候選 3 則', self.stdout.getvalue())
        for text in self.posted_texts(api):
            self.assertEqual(C.validate_draft(text), [], text)
            self.assertFalse(C.generic_comment(text), text)

        folder = self.root / 'social' / 'engagement'
        if folder.is_dir():
            for path in folder.glob('*.jsonl'):
                path.unlink()
        api = FakeThreads(scopes=scopes)
        api.search[keyword] = posts
        self.stdout = io.StringIO()

        def vary(prompt):
            line = prompt.split('對方原文：', 1)[-1].split('\n', 1)[0].strip()
            return f'{line[:8]}也太真實，你們後來怎麼辦？'

        code, _sleeps = self.execute(api, env=dict(ENV, GEMINI_API_KEY=GEMINI), complete=vary)
        self.assertEqual(code, 0, self.stdout.getvalue())
        self.assertEqual(self.posted_targets(api), ['31002', '31001'])
        self.assertTrue(any(path == '/keyword_search' for _m, path, _p, _t in api.calls))
        rows = [row for row in self.log_rows() if row['action'] == 'posted']
        self.assertEqual([row['type'] for row in rows], ['comment', 'comment'])
        self.assertTrue(all(row['text'] and row['permalink'] for row in rows))
        for row in rows:
            self.assertNotIn('theme', row)
            self.assertNotIn('label', row)
        self.assertIn('留言主題', self.stdout.getvalue())
        self.assertNotIn('maya', json.dumps(rows, ensure_ascii=False))

    def test_discovery_permission_error_keeps_replies(self):
        scopes = SCOPES + ['threads_keyword_search']
        api = FakeThreads(scopes=scopes)
        self.fixture(api)
        api.fail['/keyword_search'] = permission_error('threads_keyword_search')
        code, _sleeps = self.execute(api)
        self.assertEqual(code, 0, self.stdout.getvalue())
        self.assertIn('19001', self.posted_targets(api))
        self.assertIn('threads_keyword_search', self.stdout.getvalue())

    def test_reply_quota_stops_live_run(self):
        api = FakeThreads()
        self.fixture(api)
        api.quota['reply_quota_usage'] = 990
        code, _sleeps = self.execute(api)
        self.assertEqual(code, 1)
        self.assertEqual(api.creates, 0)
        self.assertIn('回覆額度', self.stdout.getvalue())

    def test_subsidy_reply_falls_back_instead_of_stating_a_rule(self):
        api = FakeThreads()
        api.threads = [{
            'id': '18001', 'timestamp': '2026-11-22T01:00:00+0000',
            'permalink': 'https://www.threads.com/@lovehibou/post/18001',
            'username': 'lovehibou', 'has_replies': True,
            'text': '11、12 月出生的寶寶，育兒津貼有一個 60 天關卡。\n留言報數字：你家寶寶 1 十一月出生 2 十二月出生',
        }]
        api.conversations['18001'] = [
            reply('19001', 'mama_lin', '2', 1),
            reply('19008', 'rule_parent', '津貼是不是 60 天內要申請才補領得到', 2),
        ]
        env = dict(ENV, GEMINI_API_KEY=GEMINI)
        bad = '沒錯！報完戶口記得順便把津貼申請送出去，60 天內辦好才能補領到 12 月喔。'

        def always_bad(_prompt):
            return bad

        code, _sleeps = self.execute(api, env=env, complete=always_bad)
        self.assertEqual(code, 0, self.stdout.getvalue())
        texts = self.posted_texts(api)
        self.assertEqual(len(texts), 2)
        for text in texts:
            self.assertEqual(C.validate_draft(text), [], text)
            self.assertNotIn('津貼', text)
            self.assertNotIn('60', text)
            self.assertNotIn('補領', text)
        self.assertTrue(any('十二月' in text or text in C.POLL_BARE_LINES for text in texts))
        self.assertTrue(any(text in C.OFFICIAL_CHECK_LINES for text in texts))
        self.assertNotIn(bad, self.stdout.getvalue())

    def test_dry_run_counts_discovery_candidates(self):
        scopes = SCOPES + ['threads_keyword_search']
        api = FakeThreads(scopes=scopes)
        queries = C.discovery_queries(NOW)
        first, second = queries[0]['keyword'], queries[3]['keyword']
        api.search[first] = [
            self.public_post('41001', 'lovehibou', '夜奶好累眼睛睜不開呀', '2026-11-23T00:20:00+0000', replies=True),
            self.public_post('41002', 'lin_parent', '夜奶好累眼睛睜不開，有人一樣嗎', '2026-11-23T00:10:00+0000', replies=True),
            self.public_post('41003', 'shop', '夜奶代購這陣子好多', '2026-11-23T00:05:00+0000'),
            self.public_post('41004', 'auntie', '惡婆婆又在念夜奶的事好煩', '2026-11-23T00:00:00+0000', replies=True),
            self.public_post('41005', 'market', '今天股市好高快來討論', '2026-11-22T12:00:00+0000'),
        ]
        api.search[second] = [
            self.public_post('41006', 'chen_parent', '坐月子半夜還在哄睡好累', '2026-11-22T20:00:00+0000', replies=True),
        ]
        # The second keyword may not be 坐月子; the post still has to match that keyword to be a realistic hit.
        api.search[second][0]['text'] = f'{second}真的好累，半夜還在哄'

        def vary(prompt):
            line = prompt.split('對方原文：', 1)[-1].split('\n', 1)[0].strip()
            return f'{line[:8]}也太真實，你們後來怎麼辦？'

        code, _sleeps = self.execute(api, '--dry-run', env=dict(ENV, GEMINI_API_KEY=GEMINI), complete=vary)
        self.assertEqual(code, 0, self.stdout.getvalue())
        self.assertEqual(api.creates, 0)
        output = self.stdout.getvalue()
        self.assertIn('關鍵字候選 2 則', output)
        self.assertIn('留言 2 則', output)
        self.assertIn(queries[0]['label'], output)
        self.assertIn(queries[3]['label'], output)
        self.assertEqual(self.log_rows(), [])
        self.assertNotIn('lin_parent', output)
        self.assertNotIn('chen_parent', output)


if __name__ == '__main__':
    unittest.main()
