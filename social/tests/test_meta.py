"""Facebook, Instagram and article-draft tests against a fake Graph API.

Nothing here touches the network, except one local loopback server that only
checks a token is removed from an error. The real queue must still validate.
"""
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import article_queue as A  # noqa: E402
import meta_publish as M  # noqa: E402
import publish as P  # noqa: E402
import validate as V  # noqa: E402

TAIPEI = timezone(timedelta(hours=8))
PAGE_TOKEN = 'page-token-for-tests'
IG_ID = '888'
PAGE_ID = '999'
ARTICLE = 'https://www.ihibou.com/articles/parenting-outing-bag-checklist'
IMAGE = 'https://www.ihibou.com/images/content-relay-w01/care-og-v4.png'
REPO = Path(__file__).resolve().parents[2]

THREADS_ENV = {
    'HIBOU_THREADS_TOKEN': 'fake-token-for-tests',
    'HIBOU_THREADS_USERNAME': 'lovehibou',
    'HIBOU_THREADS_USER_ID': '1234567890',
    'HIBOU_THREADS_TOKEN_ISSUED': '2026-10-20',
    'THREADS_PUBLISH_ENABLED': 'true',
}


def threads_entry():
    return {
        'id': '2026-11-24-morning-hello', 'slot': 'morning', 'publish_at': '2026-11-24T07:00:00+08:00',
        'status': 'approved', 'media_type': 'TEXT', 'text': '早安，小鷹先來打聲招呼。',
        'link': None, 'topic_tag': '育兒日常', 'self_reply': None,
        'approved_by': 'otto', 'approved_at': '2026-11-23T10:00:00+08:00',
    }


def facebook_entry(**changes):
    entry = {
        'id': '2026-11-24-facebook-outing', 'platform': 'facebook', 'slot': 'evening',
        'publish_at': '2026-11-24T18:10:00+08:00', 'status': 'approved', 'media_type': 'LINK',
        'text': '「帶寶寶出門，怎麼裝才順手？」放上喜福官網了。\n想看的人點開這篇，慢慢讀。',
        'link': ARTICLE, 'image_url': None, 'video_url': None, 'topic_tag': None, 'self_reply': None,
        'approved_by': 'otto', 'approved_at': '2026-11-23T10:00:00+08:00',
    }
    entry.update(changes)
    return entry


def instagram_entry(**changes):
    entry = {
        'id': '2026-11-24-instagram-outing', 'platform': 'instagram', 'slot': 'bedtime',
        'publish_at': '2026-11-24T21:05:00+08:00', 'status': 'approved', 'media_type': 'IMAGE',
        'text': '小鷹把「帶寶寶出門，怎麼裝才順手？」寫進官網。\n想看完整的，連結放在簡介。',
        'image_url': IMAGE, 'link': None, 'bio_link': ARTICLE, 'topic_tag': None, 'self_reply': None,
        'approved_by': 'otto', 'approved_at': '2026-11-23T10:00:00+08:00',
    }
    entry.update(changes)
    return entry


class FakeThreads:
    def __init__(self):
        self.calls = []
        self.count = 0

    @property
    def posts(self):
        return [call for call in self.calls if call[0] == 'POST']

    def __call__(self, method, path, params, token):
        assert token == THREADS_ENV['HIBOU_THREADS_TOKEN']
        self.calls.append((method, path, dict(params)))
        if path == '/me':
            return {'id': '1234567890', 'username': 'lovehibou'}
        if path.endswith('/threads_publishing_limit'):
            return {'data': [{'quota_usage': 1, 'config': {'quota_total': 250},
                              'reply_quota_usage': 0, 'reply_config': {'quota_total': 1000}}]}
        if method == 'GET' and path.endswith('/threads'):
            return {'data': []}
        if method == 'POST' and path.endswith('/threads'):
            self.count += 1
            return {'id': f'container{self.count}'}
        if method == 'POST' and path.endswith('/threads_publish'):
            return {'id': 'media-' + params['creation_id']}
        if path.startswith('/container'):
            return {'status': 'FINISHED'}
        if path.startswith('/media-'):
            return {'permalink': 'https://www.threads.com/@lovehibou/post/' + path[1:]}
        raise AssertionError(f'unexpected {method} {path}')


class FakeGraph:
    def __init__(self, page_id=PAGE_ID, ig_id=IG_ID, quota=1, recent_feed=None, fail=None):
        self.page_id, self.ig_id = page_id, ig_id
        self.quota = quota
        self.recent_feed = list(recent_feed or [])
        self.fail = fail or {}
        self.calls = []
        self.n = 0

    @property
    def posts(self):
        return [call for call in self.calls if call[0] == 'POST']

    def __call__(self, method, path, params, token):
        assert token == PAGE_TOKEN
        assert 'access' + '_token' not in params
        self.calls.append((method, path, dict(params)))
        for suffix, error in self.fail.items():
            if path == suffix or path.endswith(suffix):
                raise error
        if path == '/me':
            return {'id': self.page_id, 'name': 'HiBOU 喜福'}
        if path == '/debug_token':
            return {'data': {'application': 'HiBOU 社群串接', 'app_id': '1074864351736746', 'type': 'PAGE',
                             'expires_at': 0, 'is_valid': True,
                             'scopes': ['pages_show_list', 'pages_read_engagement', 'pages_manage_posts',
                                        'instagram_basic', 'instagram_content_publish']}}
        if path == f'/{self.page_id}/feed' and method == 'GET':
            return {'data': self.recent_feed}
        if path == f'/{self.page_id}/feed' and method == 'POST':
            return {'id': f'{self.page_id}_55'}
        if path == f'/{self.page_id}_55':
            return {'permalink_url': 'https://www.facebook.com/hibou/posts/55'}
        if path == f'/{self.ig_id}' and method == 'GET':
            return {'id': self.ig_id, 'username': 'lovehibou'}
        if path.endswith('/content_publishing_limit'):
            return {'data': [{'quota_usage': self.quota, 'config': {'quota_total': 50}}]}
        if path == f'/{self.ig_id}/media' and method == 'GET':
            return {'data': []}
        if path == f'/{self.ig_id}/media' and method == 'POST':
            self.n += 1
            return {'id': f'igc{self.n}'}
        if path == f'/{self.ig_id}/media_publish':
            return {'id': 'igmedia-' + params['creation_id']}
        if path.startswith('/igc'):
            return {'status_code': 'FINISHED'}
        if path.startswith('/igmedia-'):
            return {'permalink': 'https://www.instagram.com/p/' + path[1:]}
        raise AssertionError(f'unexpected {method} {path} {params}')


class QueueCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'social').mkdir()
        self.queue_path = self.root / 'social' / 'queue.json'

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, queue):
        self.queue_path.write_text(json.dumps(queue, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def queue(self):
        return json.loads(self.queue_path.read_text(encoding='utf-8'))

    def publish_queue(self, api, graph, when, queue, env=None, args=()):
        self.write(queue)
        merged = dict(THREADS_ENV, **(env or {}))
        return P.main(
            ['publish', *args], root=self.root, env=merged, api=api, meta_api=graph,
            clock=lambda: when, sleep=lambda _seconds: None,
            head=lambda _url: (200, 'image/png', '1200'), commit=False,
        )


class MetaHealthCheckTests(QueueCase):
    def test_check_reads_page_instagram_and_token_without_posting(self):
        import io
        from contextlib import redirect_stdout
        self.write([threads_entry()])
        graph = FakeGraph()
        env = dict(THREADS_ENV, HIBOU_FB_PAGE_TOKEN=PAGE_TOKEN, HIBOU_IG_USER_ID=IG_ID, HIBOU_FB_PAGE_ID=PAGE_ID)
        out = io.StringIO()
        with redirect_stdout(out):
            P.main(['check'], root=self.root, env=env, api=FakeThreads(), meta_api=graph,
                   clock=lambda: datetime(2026, 11, 24, 7, 10, tzinfo=TAIPEI), sleep=lambda _s: None,
                   head=lambda _url: (200, 'image/png', '1200'), commit=False)
        text = out.getvalue()
        self.assertIn('Meta 檢查：粉專 HiBOU 喜福（id 999）', text)
        self.assertIn('token 來自 HiBOU 社群串接，類型 PAGE，到期 不會過期', text)
        self.assertIn('Meta 檢查：Instagram @lovehibou', text)
        self.assertNotIn('缺少', text)
        self.assertNotIn(PAGE_TOKEN, text)
        self.assertEqual(graph.posts, [])


class PublishMetaTests(QueueCase):
    def test_missing_page_token_skips_facebook_and_still_publishes_threads(self):
        graph = FakeGraph()
        threads = FakeThreads()
        when = datetime(2026, 11, 24, 7, 45, tzinfo=TAIPEI)
        code = self.publish_queue(threads, graph, when, [threads_entry(), facebook_entry()], env={
            'FB_PUBLISH_ENABLED': 'true',
        })
        self.assertEqual(code, 0)
        self.assertEqual(len(threads.posts), 2)
        self.assertEqual(graph.calls, [])
        saved = {entry['id']: entry for entry in self.queue()}
        self.assertEqual(saved['2026-11-24-morning-hello']['status'], 'published')
        self.assertEqual(saved['2026-11-24-facebook-outing']['status'], 'approved')

    def test_facebook_link_post_goes_out_when_enabled(self):
        graph = FakeGraph()
        when = datetime(2026, 11, 24, 18, 45, tzinfo=TAIPEI)
        code = self.publish_queue(FakeThreads(), graph, when, [facebook_entry()], env={
            'FB_PUBLISH_ENABLED': 'true', 'HIBOU_FB_PAGE_TOKEN': PAGE_TOKEN, 'HIBOU_FB_PAGE_ID': PAGE_ID,
        })
        self.assertEqual(code, 0)
        posted = [call for call in graph.posts if call[1].endswith('/feed')]
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][2]['link'], ARTICLE)
        self.assertIn('喜福官網', posted[0][2]['message'])
        entry = self.queue()[0]
        self.assertEqual(entry['status'], 'published')
        self.assertEqual(entry['published_id'], f'{PAGE_ID}_55')
        self.assertTrue(entry['permalink'].startswith('https://www.facebook.com/'))
        receipt = (self.root / 'social' / 'receipts' / '2026-11.jsonl').read_text(encoding='utf-8')
        self.assertIn('"platform": "facebook"', receipt)
        self.assertNotIn(PAGE_TOKEN, receipt)

    def test_switch_off_is_dry_run(self):
        graph = FakeGraph()
        when = datetime(2026, 11, 24, 18, 45, tzinfo=TAIPEI)
        before = json.dumps([facebook_entry()])
        self.write(json.loads(before))
        code = self.publish_queue(FakeThreads(), graph, when, [facebook_entry()], env={'FB_PUBLISH_ENABLED': 'false'})
        self.assertEqual(code, 0)
        self.assertEqual(graph.calls, [])
        self.assertEqual(self.queue()[0]['status'], 'approved')

    def test_live_facebook_from_another_branch_stays_dry(self):
        graph = FakeGraph()
        when = datetime(2026, 11, 24, 18, 45, tzinfo=TAIPEI)
        code = self.publish_queue(FakeThreads(), graph, when, [facebook_entry()], env={
            'THREADS_PUBLISH_ENABLED': 'false',
            'FB_PUBLISH_ENABLED': 'true',
            'HIBOU_FB_PAGE_TOKEN': PAGE_TOKEN,
            'GITHUB_REF_NAME': 'feature/not-main',
        })
        self.assertEqual(code, 0)
        self.assertEqual(graph.posts, [])
        self.assertEqual(self.queue()[0]['status'], 'approved')

    def test_wrong_page_sends_nothing(self):
        graph = FakeGraph(page_id='000')
        when = datetime(2026, 11, 24, 18, 45, tzinfo=TAIPEI)
        code = self.publish_queue(FakeThreads(), graph, when, [facebook_entry()], env={
            'FB_PUBLISH_ENABLED': 'true', 'HIBOU_FB_PAGE_TOKEN': PAGE_TOKEN, 'HIBOU_FB_PAGE_ID': PAGE_ID,
        })
        self.assertEqual(code, 1)
        self.assertEqual(graph.posts, [])
        self.assertEqual(self.queue()[0]['status'], 'approved')

    def test_instagram_single_image_then_publish(self):
        graph = FakeGraph()
        when = datetime(2026, 11, 24, 21, 40, tzinfo=TAIPEI)
        code = self.publish_queue(FakeThreads(), graph, when, [instagram_entry()], env={
            'IG_PUBLISH_ENABLED': 'true', 'HIBOU_FB_PAGE_TOKEN': PAGE_TOKEN, 'HIBOU_IG_USER_ID': IG_ID,
        })
        self.assertEqual(code, 0)
        media = [call for call in graph.posts if call[1].endswith('/media')]
        publish = [call for call in graph.posts if call[1].endswith('/media_publish')]
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0][2]['image_url'], IMAGE)
        self.assertNotIn('http', media[0][2]['caption'])
        self.assertIn('連結放在簡介', media[0][2]['caption'])
        self.assertEqual(publish[0][2]['creation_id'], 'igc1')
        self.assertEqual(self.queue()[0]['status'], 'published')

    def test_instagram_carousel_publishes_children_before_the_parent(self):
        graph = FakeGraph()
        images = [
            'https://www.ihibou.com/images/a.jpg',
            'https://www.ihibou.com/images/b.jpg',
        ]
        entry = instagram_entry(media_type='CAROUSEL', image_url=None, image_urls=images)
        when = datetime(2026, 11, 24, 21, 40, tzinfo=TAIPEI)
        code = self.publish_queue(FakeThreads(), graph, when, [entry], env={
            'IG_PUBLISH_ENABLED': 'true', 'HIBOU_FB_PAGE_TOKEN': PAGE_TOKEN, 'HIBOU_IG_USER_ID': IG_ID,
        })
        self.assertEqual(code, 0)
        media = [call[2] for call in graph.posts if call[1].endswith('/media')]
        self.assertEqual([item.get('image_url') for item in media[:2]], images)
        self.assertEqual(media[0]['is_carousel_item'], 'true')
        self.assertNotIn('caption', media[0])
        self.assertEqual(media[2]['media_type'], 'CAROUSEL')
        self.assertEqual(media[2]['children'], 'igc1,igc2')
        self.assertIn('連結放在簡介', media[2]['caption'])

    def test_duplicate_first_line_is_not_posted_again(self):
        text = facebook_entry()['text']
        graph = FakeGraph(recent_feed=[{
            'id': '999_9', 'message': text, 'permalink_url': 'https://www.facebook.com/hibou/posts/9',
            'created_time': '2026-11-24T10:00:00+0800',
        }])
        when = datetime(2026, 11, 24, 18, 45, tzinfo=TAIPEI)
        code = self.publish_queue(FakeThreads(), graph, when, [facebook_entry()], env={
            'FB_PUBLISH_ENABLED': 'true', 'HIBOU_FB_PAGE_TOKEN': PAGE_TOKEN,
        })
        self.assertEqual(code, 0)
        self.assertEqual([call for call in graph.posts if call[1].endswith('/feed')], [])
        self.assertEqual(self.queue()[0]['status'], 'published')
        self.assertEqual(self.queue()[0]['published_id'], '999_9')

    def test_missing_instagram_id_does_not_fail_the_run(self):
        graph = FakeGraph()
        when = datetime(2026, 11, 24, 21, 40, tzinfo=TAIPEI)
        code = self.publish_queue(FakeThreads(), graph, when, [instagram_entry()], env={
            'IG_PUBLISH_ENABLED': 'true', 'HIBOU_FB_PAGE_TOKEN': PAGE_TOKEN,
        })
        self.assertEqual(code, 0)
        self.assertEqual(graph.calls, [])
        self.assertEqual(self.queue()[0]['status'], 'approved')

    def test_graph_error_does_not_contain_the_token(self):
        self.assertNotIn(PAGE_TOKEN, M._scrub('bad ' + PAGE_TOKEN + ' here', PAGE_TOKEN))

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                query = urllib.parse.urlsplit(self.path).query
                secret = urllib.parse.parse_qs(query).get('access' + '_token', [''])[0]
                body = ('leaked ' + secret).encode()
                self.send_response(400)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        original = M.GRAPH
        M.GRAPH = f'http://{host}:{port}'
        try:
            with self.assertRaises(M.GraphError) as caught:
                M.graph('GET', '/me', {'fields': 'id'}, PAGE_TOKEN)
        finally:
            M.GRAPH = original
            server.shutdown()
        self.assertNotIn(PAGE_TOKEN, str(caught.exception))
        self.assertNotIn(PAGE_TOKEN, caught.exception.body)


class ValidateMetaTests(unittest.TestCase):
    def test_facebook_and_instagram_examples_pass(self):
        self.assertEqual(V.validate_entry(facebook_entry()), [])
        self.assertEqual(V.validate_entry(instagram_entry()), [])
        images = ['https://www.ihibou.com/images/a.jpg', 'https://www.ihibou.com/images/b.png']
        carousel = instagram_entry(media_type='CAROUSEL', image_url=None, image_urls=images)
        self.assertEqual(V.validate_entry(carousel), [])

    def test_guardrails(self):
        banned = {
            'hashtag': '今天來看看 #育兒日常',
            '您': '希望對您有幫助',
            'price': '這條 350 元',
            'stock': '現貨只剩 3 條',
            'lead': '交期大約一週',
            'medical': '可以治療濕疹',
            'japan': '日本製的紗布',
            'japan-design': '日本設計',
            'japan-sales': '日本熱銷',
            'ai': '這篇是 AI 製作',
            'service': '親愛的家長您好',
        }
        for name, text in banned.items():
            problems = V.meta_copy_problems(text + '\n連結放在簡介。', 'instagram')
            self.assertTrue(problems, name)
        gift = V.meta_copy_problems('送禮日想指定日期。\n連結放在簡介。', 'instagram')
        self.assertTrue(any('送禮日' in problem or 'gift' in problem for problem in gift))
        ok = '有送禮日的話，先用 LINE 問我們\n連結放在簡介。'
        self.assertEqual(V.meta_copy_problems(ok, 'instagram'), [])
        listed = '1. 先看尿布\n2. 再看衣服\n連結放在簡介。'
        self.assertTrue(any('list' in problem for problem in V.meta_copy_problems(listed, 'instagram')))

    def test_instagram_cannot_carry_a_url_or_skip_the_bio_line(self):
        text = '看這篇 https://www.ihibou.com/articles/parenting-outing-bag-checklist'
        problems = V.validate_entry(instagram_entry(text=text))
        self.assertTrue(any('link' in problem or '簡介' in problem for problem in problems))

    def test_facebook_link_must_be_an_article(self):
        problems = V.validate_entry(facebook_entry(link='https://www.ihibou.com/products/hibou-bib'))
        self.assertTrue(any('facebook link' in problem for problem in problems))
        problems = V.validate_entry(facebook_entry(link=ARTICLE + '?utm_source=fb'))
        self.assertTrue(problems)

    def test_existing_threads_queue_still_validates(self):
        queue = json.loads((REPO / 'social' / 'queue.json').read_text(encoding='utf-8'))
        self.assertEqual(V.validate_queue(queue, REPO), [])

    def test_bad_facebook_draft_blocks_threads_publish(self):
        case = QueueCase()
        case.setUp()
        try:
            bad = facebook_entry(text='這則有 #內部標籤')
            code = case.publish_queue(FakeThreads(), FakeGraph(), datetime(2026, 11, 24, 7, 45, tzinfo=TAIPEI),
                            [threads_entry(), bad])
            self.assertEqual(code, 1)
            self.assertEqual(case.queue()[0]['status'], 'approved')
        finally:
            case.tearDown()


OUTING_HTML = """
<html><head>
<meta property="og:title" content="寶寶外出包清單｜帶寶寶出門，怎麼裝才順手？｜HiBOU 喜福">
<meta name="description" content="帶寶寶出門要帶什麼？依行程把尿布和替換衣物放好。">
<meta property="og:image" content="https://www.ihibou.com/images/content-relay-w01/care-og-v4.png">
</head></html>
"""

GIFT_HTML = """
<html><head>
<meta property="og:title" content="彌月禮什麼時候準備｜HiBOU 喜福">
<meta property="og:description" content="有人會問趕不趕得及，先別自己答應日期。">
<meta property="og:image" content="https://www.ihibou.com/images/a.jpg">
<meta property="og:image" content="https://www.ihibou.com/images/b.jpg">
</head></html>
"""


class ArticleQueueTests(unittest.TestCase):
    def fetch(self, url, method='GET'):
        if url.endswith(('.png', '.jpg')):
            return 200, url, b'', 'image/jpeg' if url.endswith('.jpg') else 'image/png'
        if url.endswith('/parenting-outing-bag-checklist'):
            return 200, url, OUTING_HTML.encode(), 'text/html'
        if url.endswith('/gift-timing'):
            return 200, url, GIFT_HTML.encode(), 'text/html'
        if 'japan' in url:
            page = """
            <html><head>
            <meta property="og:title" content="日本製紗布怎麼挑｜HiBOU 喜福">
            <meta property="og:image" content="https://www.ihibou.com/images/content-relay-w01/care-og-v4.png">
            </head></html>
            """
            return 200, url, page.encode(), 'text/html'
        return 404, url, b'missing', 'text/html'

    def test_live_article_becomes_three_distinct_drafts(self):
        now = datetime(2026, 11, 23, 16, 0, tzinfo=TAIPEI)
        entries, messages = A.plan([ARTICLE], [], now, self.fetch, REPO)
        self.assertEqual(len(entries), 3, messages)
        platforms = [entry['platform'] for entry in entries]
        self.assertEqual(platforms, ['threads', 'facebook', 'instagram'])
        self.assertEqual(len({entry['text'] for entry in entries}), 3)
        minutes = [entry['publish_at'][11:16] for entry in entries]
        self.assertEqual(minutes, ['07:05', '12:20', '12:25'])
        self.assertEqual({entry['publish_at'][:10] for entry in entries}, {'2026-11-25'})  # Wednesday
        for entry in entries:
            hour, minute = entry['publish_at'][11:16].split(':')
            self.assertNotIn((int(hour), int(minute)), A.FORBIDDEN_MINUTES)
            self.assertEqual(entry['status'], 'draft')
        self.assertEqual(entries[1]['link'], ARTICLE)
        self.assertNotIn('http', entries[2]['text'])
        self.assertIn('連結放在簡介', entries[2]['text'])
        self.assertEqual(entries[2]['bio_link'], ARTICLE)
        self.assertEqual(entries[2]['image_url'], IMAGE)
        problems = V.validate_queue(entries, REPO)
        self.assertEqual(problems, [], problems)
        self.assertTrue(any('三則草稿' in line for line in messages))

    def test_articles_skip_tuesday_thursday_and_busy_dates(self):
        from datetime import date
        now = datetime(2026, 11, 23, 16, 0, tzinfo=TAIPEI)
        entries, _messages = A.plan([ARTICLE], [], now, self.fetch, REPO, busy={date(2026, 11, 25)})
        self.assertEqual({entry['publish_at'][:10] for entry in entries}, {'2026-11-27'})
        for entry in entries:
            day = datetime.fromisoformat(entry['publish_at']).date()
            self.assertIn(day.weekday(), A.ARTICLE_WEEKDAYS)

    def test_dead_link_is_skipped(self):
        now = datetime(2026, 11, 23, 16, 0, tzinfo=TAIPEI)
        entries, messages = A.plan(['https://www.ihibou.com/articles/missing-page'], [], now, self.fetch, REPO)
        self.assertEqual(entries, [])
        self.assertTrue(any(line.startswith('跳過：') for line in messages))

    def test_second_article_moves_to_the_next_day_and_gift_copy_is_fixed(self):
        now = datetime(2026, 11, 23, 16, 0, tzinfo=TAIPEI)
        urls = [ARTICLE, 'https://www.ihibou.com/journals/gift-timing']
        entries, messages = A.plan(urls, [], now, self.fetch, REPO)
        self.assertEqual(len(entries), 6, messages)
        days = {entry['publish_at'][:10] for entry in entries}
        self.assertEqual(days, {'2026-11-25', '2026-11-27'})  # Wednesday, Friday
        gift = [entry for entry in entries if entry['platform'] != 'threads' and 'gift-timing' in entry['id']]
        for entry in gift:
            self.assertIn(V.GIFT_DATE_LINE, entry['text'])
        carousel = next(entry for entry in entries if entry['id'].endswith('instagram-gift-timing'))
        self.assertEqual(carousel['media_type'], 'CAROUSEL')
        self.assertEqual(len(carousel['image_urls']), 2)
        self.assertEqual(V.validate_queue(entries, REPO), [])

    def test_japan_claim_in_the_title_is_not_queued(self):
        now = datetime(2026, 11, 23, 16, 0, tzinfo=TAIPEI)
        entries, messages = A.plan(['https://www.ihibou.com/articles/japan-gauze'], [], now, self.fetch, REPO)
        self.assertEqual(entries, [])
        self.assertTrue(any('跳過' in line for line in messages))

    def test_write_appends_drafts_without_reordering_the_old_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'social').mkdir()
            old = [threads_entry()]
            (root / 'social' / 'queue.json').write_text(json.dumps(old, ensure_ascii=False) + '\n', encoding='utf-8')
            code = A.main([ARTICLE, '--write', '--root', str(root)], fetch=self.fetch,
                          clock=lambda: datetime(2026, 11, 23, 16, 0, tzinfo=TAIPEI))
            self.assertEqual(code, 0)
            saved = json.loads((root / 'social' / 'queue.json').read_text(encoding='utf-8'))
            self.assertEqual(saved[0]['id'], old[0]['id'])
            self.assertEqual(saved[0]['text'], old[0]['text'])
            self.assertEqual([entry['platform'] for entry in saved[1:]], ['threads', 'facebook', 'instagram'])


if __name__ == '__main__':
    unittest.main()
