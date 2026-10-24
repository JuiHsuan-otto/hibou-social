"""Tests for publish.py and validate.py against a fake Threads API.

    python3 -m unittest discover -s social/tests -v

Nothing here touches the network or git.
"""
import hashlib
import json
import struct
import sys
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import publish as P  # noqa: E402
import validate as V  # noqa: E402

TAIPEI = timezone(timedelta(hours=8))
USER_ID = '1234567890'
ENV = {
    'HIBOU_THREADS_TOKEN': 'fake-token-for-tests',
    'HIBOU_THREADS_USERNAME': 'lovehibou',
    'HIBOU_THREADS_USER_ID': USER_ID,
    'HIBOU_THREADS_TOKEN_ISSUED': '2026-10-20',
    'THREADS_PUBLISH_ENABLED': 'true',
}


def png_bytes(width, height):
    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xFFFFFFFF)
    raw = b''.join(b'\x00' + b'\x00' * width * 3 for _ in range(height))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def jpeg_bytes(width, height):
    sof = b'\xff\xc0' + struct.pack('>HBHHB', 11, 8, height, width, 1) + b'\x01\x11\x00'
    return b'\xff\xd8' + b'\xff\xe0' + struct.pack('>H', 16) + b'JFIF\x00' + b'\x00' * 9 + sof + b'\xff\xd9'


class FakeThreads:
    """Records every call; answers like the Threads API."""

    def __init__(self, username='lovehibou', user_id=USER_ID, recent=None, quota=10, fail=None, statuses=None):
        self.username, self.user_id = username, user_id
        self.recent = list(recent or [])
        self.quota = quota
        self.fail = fail or {}            # path suffix -> ApiError to raise
        self.statuses = statuses           # container status answers, None = FINISHED
        self.calls = []
        self.count = 0

    @property
    def posts(self):
        return [c for c in self.calls if c[0] == 'POST']

    def __call__(self, method, path, params, token):
        assert token == ENV['HIBOU_THREADS_TOKEN']
        self.calls.append((method, path, dict(params)))
        for suffix, error in self.fail.items():
            if path.endswith(suffix):
                raise error
        if path == '/me':
            return {'id': self.user_id, 'username': self.username}
        if path.endswith('/threads_publishing_limit'):
            return {'data': [{'quota_usage': self.quota, 'config': {'quota_total': 250},
                              'reply_quota_usage': 0, 'reply_config': {'quota_total': 1000}}]}
        if method == 'GET' and path.endswith('/threads'):
            return {'data': self.recent}
        if method == 'POST' and path.endswith('/threads'):
            self.count += 1
            return {'id': f'container{self.count}'}
        if method == 'POST' and path.endswith('/threads_publish'):
            return {'id': 'media-' + params['creation_id']}
        if path.startswith('/container'):
            if self.statuses:
                return self.statuses.pop(0)
            return {'status': 'FINISHED'}
        if path.startswith('/media-'):
            return {'permalink': 'https://www.threads.com/@lovehibou/post/' + path[1:]}
        raise AssertionError(f'unexpected call {method} {path}')


def auth_error():
    return P.ApiError('GET', '/me', 400, json.dumps({'error': {'message': 'Error validating access token: Session has expired',
                                                               'type': 'OAuthException', 'code': 190}}))


class Base(unittest.TestCase):
    now = datetime(2026, 10, 30, 21, 35, tzinfo=TAIPEI)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'social' / 'images' / '2026').mkdir(parents=True)
        self.image = jpeg_bytes(1080, 1350)
        (self.root / 'social/images/2026/1030-bedtime.jpg').write_bytes(self.image)
        self.write_queue([self.entry()])

    def tearDown(self):
        self.tmp.cleanup()

    def entry(self, **changes):
        entry = {
            'id': '2026-10-30-bedtime', 'slot': 'bedtime', 'publish_at': '2026-10-30T21:30:00+08:00',
            'status': 'approved', 'media_type': 'IMAGE',
            'text': '萬聖節前夕，推車裡突然冒出一隻兔子。\n幫這張圖配一句台詞。',
            'image_url': V.RAW_BASE + 'social/images/2026/1030-bedtime.jpg',
            'image_sha256': hashlib.sha256(self.image).hexdigest(),
            'link': None, 'topic_tag': '育兒日常', 'self_reply': None, 'pin_after_post': False,
            'source': 'schedule.json#2026-10-30', 'approved_by': 'claude (CLAUDE_AUTHORIZATION §6)',
            'approved_at': '2026-10-24T15:00:00+08:00',
        }
        entry.update(changes)
        return entry

    def write_queue(self, queue):
        self.queue_path = self.root / 'social' / 'queue.json'
        self.queue_path.write_text(json.dumps(queue, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        self.queue_before = self.queue_path.read_bytes()

    def queue(self):
        return json.loads(self.queue_path.read_text(encoding='utf-8'))

    def receipts(self):
        path = self.root / 'social' / 'receipts' / '2026-10.jsonl'
        return [json.loads(l) for l in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []

    def run_publish(self, api, *args, env=None, now=None):
        env = dict(ENV, **(env or {}))
        return P.main(['publish', *args], root=self.root, env=env, api=api, clock=lambda: now or self.now,
                      sleep=lambda s: None, head=lambda url: (200, 'image/jpeg', '1234'), commit=False)


class PreflightTests(Base):
    def test_wrong_account_stops_without_any_post_or_write(self):
        api = FakeThreads(username='deeptutor.tw')
        self.assertEqual(self.run_publish(api), 1)
        self.assertEqual(api.posts, [])
        self.assertEqual(self.queue_path.read_bytes(), self.queue_before)
        self.assertEqual(self.receipts(), [])

    def test_wrong_user_id_stops(self):
        api = FakeThreads(user_id='999')
        self.assertEqual(self.run_publish(api), 1)
        self.assertEqual(api.posts, [])
        self.assertEqual(self.queue_path.read_bytes(), self.queue_before)

    def test_expired_token_skips_whole_batch(self):
        api = FakeThreads(fail={'/me': auth_error()})
        self.assertEqual(self.run_publish(api), 1)
        self.assertEqual(api.posts, [])
        self.assertEqual(self.queue_path.read_bytes(), self.queue_before)

    def test_quota_nearly_used_stops(self):
        api = FakeThreads(quota=240)
        self.assertEqual(self.run_publish(api), 1)
        self.assertEqual(api.posts, [])

    def test_invalid_queue_sends_nothing(self):
        self.write_queue([self.entry(), self.entry(id='2026-10-31-bedtime', text='這條有 UPF 50+')])
        api = FakeThreads()
        self.assertEqual(self.run_publish(api), 1)
        self.assertEqual(api.posts, [])

    def test_check_mode_is_read_only(self):
        api = FakeThreads()
        code = P.main(['check'], root=self.root, env=dict(ENV), api=api, clock=lambda: self.now,
                      sleep=lambda s: None, head=None, commit=False)
        self.assertEqual(code, 0)
        self.assertEqual(api.posts, [])
        self.assertEqual(self.queue_path.read_bytes(), self.queue_before)


class PublishTests(Base):
    def test_publishes_due_entry_and_records_it(self):
        api = FakeThreads()
        self.assertEqual(self.run_publish(api), 0)
        create, publish = api.posts
        self.assertEqual(create[2]['media_type'], 'IMAGE')
        self.assertEqual(create[2]['topic_tag'], '育兒日常')
        self.assertEqual(publish[2], {'creation_id': 'container1'})
        entry = self.queue()[0]
        self.assertEqual(entry['status'], 'published')
        self.assertEqual(entry['published_id'], 'media-container1')
        self.assertTrue(entry['permalink'].endswith('media-container1'))
        self.assertEqual([r['action'] for r in self.receipts()], ['post'])

    def test_waits_for_container_before_publishing(self):
        api = FakeThreads(statuses=[{'status': 'IN_PROGRESS'}, {'status': 'FINISHED'}])
        self.assertEqual(self.run_publish(api), 0)
        polls = [c for c in api.calls if c[1] == '/container1']
        self.assertEqual(len(polls), 2)

    def test_container_error_marks_failed_without_publish(self):
        api = FakeThreads(statuses=[{'status': 'ERROR', 'error_message': 'bad image'}])
        self.assertEqual(self.run_publish(api), 1)
        self.assertEqual([c[1] for c in api.posts], [f'/{USER_ID}/threads'])
        self.assertEqual(self.queue()[0]['status'], 'failed')
        self.assertEqual(self.receipts()[0]['action'], 'fail')

    def test_stuck_publishing_is_never_retried(self):
        self.write_queue([self.entry(status='publishing')])
        api = FakeThreads()
        self.assertEqual(self.run_publish(api), 0)
        self.assertEqual(api.posts, [])
        self.assertEqual(self.queue()[0]['status'], 'publishing')

    def test_existing_post_with_same_first_line_is_not_sent_again(self):
        recent = [{'id': '777', 'text': '萬聖節前夕，推車裡突然冒出一隻兔子。\n（Chrome 發的）',
                   'timestamp': '2026-10-30T13:31:00+0000', 'permalink': 'https://www.threads.com/@lovehibou/post/x'}]
        api = FakeThreads(recent=recent)
        self.assertEqual(self.run_publish(api), 0)
        self.assertEqual(api.posts, [])
        entry = self.queue()[0]
        self.assertEqual(entry['status'], 'published')
        self.assertEqual(entry['published_id'], '777')
        self.assertEqual(self.receipts()[0]['action'], 'skip')

    def test_second_run_of_same_entry_is_blocked(self):
        api = FakeThreads()
        self.assertEqual(self.run_publish(api, '--entry-id', '2026-10-30-bedtime'), 0)
        posts_after_first = len(api.posts)
        self.assertEqual(self.run_publish(api, '--entry-id', '2026-10-30-bedtime'), 0)
        self.assertEqual(len(api.posts), posts_after_first)

    def test_outside_window_is_skipped_not_sent(self):
        api = FakeThreads()
        late = datetime(2026, 10, 30, 23, 40, tzinfo=TAIPEI)
        self.assertEqual(self.run_publish(api, now=late), 0)
        self.assertEqual(api.posts, [])
        self.assertEqual(self.queue()[0]['status'], 'skipped')
        self.assertEqual(self.receipts()[0]['note'], 'missed window')

    def test_trend_window_is_90_minutes(self):
        entry = self.entry(id='2026-10-30-noon', slot='noon', publish_at='2026-10-30T12:30:00+08:00')
        self.write_queue([entry])
        api = FakeThreads()
        self.run_publish(api, now=datetime(2026, 10, 30, 14, 1, tzinfo=TAIPEI))
        self.assertEqual(self.queue()[0]['status'], 'skipped')

    def test_not_yet_due_is_left_alone(self):
        api = FakeThreads()
        self.assertEqual(self.run_publish(api, now=datetime(2026, 10, 30, 21, 0, tzinfo=TAIPEI)), 0)
        self.assertEqual(api.posts, [])
        self.assertEqual(self.queue_path.read_bytes(), self.queue_before)

    def test_daily_cap_of_four_top_level_posts(self):
        recent = [{'id': str(i), 'text': f'早安貼文 {i}', 'timestamp': f'2026-10-30T0{i}:00:00+0000', 'permalink': ''}
                  for i in range(4)]
        api = FakeThreads(recent=recent)
        self.assertEqual(self.run_publish(api), 1)
        self.assertEqual(api.posts, [])
        self.assertEqual(self.queue()[0]['status'], 'approved')

    def test_test_posts_do_not_count_toward_cap(self):
        recent = [{'id': str(i), 'text': f'早安貼文 {i}', 'timestamp': f'2026-10-30T0{i}:00:00+0000', 'permalink': ''}
                  for i in range(4)]
        self.write_queue([self.entry(id='t1', slot='test', approved_by='otto')])
        api = FakeThreads(recent=recent)
        self.assertEqual(self.run_publish(api, '--entry-id', 't1'), 0)
        self.assertEqual(len(api.posts), 2)

    def test_self_reply_goes_under_the_new_post(self):
        self.write_queue([self.entry(self_reply={'text': '延伸看：https://www.ihibou.com/articles/parenting-bib-guide'})])
        api = FakeThreads()
        self.assertEqual(self.run_publish(api), 0)
        reply_create = api.posts[2]
        self.assertEqual(reply_create[2]['reply_to_id'], 'media-container1')
        self.assertEqual(reply_create[2]['media_type'], 'TEXT')
        self.assertEqual(self.queue()[0]['reply_id'], 'media-container2')
        self.assertEqual([r['action'] for r in self.receipts()], ['post', 'self_reply'])

    def test_failed_self_reply_keeps_main_post_published(self):
        self.write_queue([self.entry(self_reply={'text': '延伸看：https://www.ihibou.com/articles/parenting-bib-guide'})])
        api = FakeThreads(statuses=[{'status': 'FINISHED'}, {'status': 'ERROR', 'error_message': 'x'}])
        self.assertEqual(self.run_publish(api), 0)
        self.assertEqual(self.queue()[0]['status'], 'published')
        self.assertEqual([r['action'] for r in self.receipts()], ['post', 'fail'])

    def test_pin_request_is_noted(self):
        self.write_queue([self.entry(pin_after_post=True)])
        self.run_publish(FakeThreads())
        self.assertTrue(self.queue()[0]['needs_pin'])


class SafetyTests(Base):
    def test_dry_run_sends_and_writes_nothing(self):
        api = FakeThreads()
        self.assertEqual(self.run_publish(api, '--dry-run'), 0)
        self.assertEqual(api.posts, [])
        self.assertEqual(self.queue_path.read_bytes(), self.queue_before)
        self.assertFalse((self.root / 'social' / 'receipts').exists())

    def test_kill_switch_forces_dry_run(self):
        api = FakeThreads()
        self.assertEqual(self.run_publish(api, env={'THREADS_PUBLISH_ENABLED': 'false'}), 0)
        self.assertEqual(api.posts, [])
        self.assertEqual(self.queue_path.read_bytes(), self.queue_before)

    def test_kill_switch_lets_otto_approved_test_through(self):
        self.write_queue([self.entry(id='t1', slot='test', approved_by='otto')])
        api = FakeThreads()
        self.assertEqual(self.run_publish(api, '--entry-id', 't1', env={'THREADS_PUBLISH_ENABLED': 'false'}), 0)
        self.assertEqual(len(api.posts), 2)

    def test_kill_switch_blocks_test_without_entry_id(self):
        self.write_queue([self.entry(id='t1', slot='test', approved_by='otto')])
        api = FakeThreads()
        self.run_publish(api, env={'THREADS_PUBLISH_ENABLED': 'false'})
        self.assertEqual(api.posts, [])

    def test_publishing_only_from_main(self):
        api = FakeThreads()
        self.assertEqual(self.run_publish(api, env={'GITHUB_REF_NAME': 'claude/threads-api-20261024'}), 1)
        self.assertEqual(api.posts, [])

    def test_token_never_appears_in_errors(self):
        error = P.ApiError('POST', f'/{USER_ID}/threads', 500, '{"error":{"message":"boom"}}')
        self.assertNotIn(ENV['HIBOU_THREADS_TOKEN'], str(error))

    def test_unknown_entry_id_aborts(self):
        api = FakeThreads()
        self.assertEqual(self.run_publish(api, '--entry-id', 'nope'), 1)
        self.assertEqual(api.posts, [])

    def test_bad_entry_id_characters_rejected(self):
        self.assertEqual(self.run_publish(FakeThreads(), '--entry-id', 'x; rm -rf'), 1)


class ValidateTests(Base):
    def problems(self, **changes):
        return V.validate_entry(self.entry(**changes), self.root)

    def test_good_entry(self):
        self.assertEqual(self.problems(), [])

    def test_required_fields(self):
        self.assertTrue(any('missing text' in p or 'text is empty' in p for p in self.problems(text='')))

    def test_publish_at_needs_taipei_offset(self):
        self.assertTrue(self.problems(publish_at='2026-10-30T13:30:00Z'))

    def test_duplicate_ids(self):
        problems = V.validate_queue([self.entry(), self.entry()], self.root)
        self.assertIn(('2026-10-30-bedtime', 'duplicate id'), problems)

    def test_length_limit(self):
        self.assertTrue(self.problems(text='哈' * 501))
        self.assertEqual(self.problems(text='哈' * 500), [])

    def test_no_emoji(self):
        self.assertTrue(any('emoji' in p for p in self.problems(text='好可愛😂')))

    def test_banned_words_case_insensitive(self):
        for text in ('防曬 upf 50', '抗菌布料', '日本製', '保證好用', '這張是 AI 製作'):
            self.assertTrue(self.problems(text=text), text)

    def test_ordinary_words_are_not_banned(self):
        self.assertEqual(self.problems(text='有機會的話，留言告訴小鷹'), [])

    def test_links_only_to_ihibou(self):
        self.assertTrue(self.problems(text='看這裡 https://shopee.tw/x'))
        self.assertEqual(self.problems(text='看這裡 https://www.ihibou.com/products/hibou-custom#name-tag'), [])

    def test_at_most_five_links(self):
        links = ' '.join(f'https://www.ihibou.com/p{i}' for i in range(6))
        self.assertTrue(any('distinct links' in p for p in self.problems(text=links)))

    def test_image_rules(self):
        self.assertTrue(self.problems(image_url='https://example.com/a.jpg'))
        self.assertTrue(any('sha256' in p for p in self.problems(image_sha256='0' * 64)))
        self.assertTrue(any('missing' in p for p in self.problems(image_url=V.IMAGE_PREFIX + '2026/none.jpg')))
        narrow = png_bytes(300, 400)
        (self.root / 'social/images/2026/narrow.png').write_bytes(narrow)
        problems = self.problems(image_url=V.IMAGE_PREFIX + '2026/narrow.png', image_sha256=hashlib.sha256(narrow).hexdigest())
        self.assertTrue(any('width 300px' in p for p in problems))

    def test_image_size_detection(self):
        self.assertEqual(V.image_info(png_bytes(320, 200)), ('PNG', 320, 200))
        self.assertEqual(V.image_info(jpeg_bytes(1080, 1350)), ('JPEG', 1080, 1350))
        self.assertIsNone(V.image_info(b'GIF89a....'))

    def test_topic_tag_rules(self):
        self.assertTrue(self.problems(topic_tag='a.b'))
        self.assertTrue(self.problems(topic_tag='a&b'))
        self.assertTrue(self.problems(topic_tag='字' * 51))
        self.assertEqual(self.problems(topic_tag='帶娃出門崩潰日記'), [])

    def test_link_attachment_only_for_text(self):
        self.assertTrue(any('only be used with media_type TEXT' in p for p in self.problems(link='https://www.ihibou.com/')))
        self.assertEqual(self.problems(media_type='TEXT', image_url=None, image_sha256=None, link='https://www.ihibou.com/'), [])

    def test_self_reply_link_must_be_on_playbook_list(self):
        self.assertTrue(self.problems(self_reply={'text': '延伸看：https://www.ihibou.com/checkout.html'}))
        self.assertEqual(self.problems(self_reply={'text': '延伸看：https://www.ihibou.com/parenting-notes?utm_source=threads'}), [])

    def test_test_posts_need_otto(self):
        self.assertTrue(self.problems(slot='test'))
        self.assertEqual(self.problems(slot='test', approved_by='otto'), [])

    def test_finished_entries_only_get_structural_checks(self):
        self.assertEqual(self.problems(status='posted_by_chrome', text='舊貼文 😂', image_url=None), [])


if __name__ == '__main__':
    unittest.main()
