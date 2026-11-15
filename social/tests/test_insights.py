"""Unit tests for the read-only Threads insights pull. No network, no token."""
from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'social'))
import insights as I  # noqa: E402


QUEUE = [
    {
        'id': '2026-11-10-evening-owl-text-10',
        'topic_tag': '育兒日常',
        'published_id': '18083115968422426',
        'text': '今天出門先別急。\nhttps://www.ihibou.com/articles/parenting-outing-bag-checklist?utm_source=threads&utm_medium=organic_social&utm_campaign=threads_winter_20261221',
        'status': 'published',
    },
    {
        'id': '2026-11-16-noon-draft',
        'topic_tag': '彌月禮',
        'status': 'approved',
        'text': '還沒發。',
    },
]


class InsightsHelpers(unittest.TestCase):
    def test_extracts_campaign_and_published_targets(self):
        receipts = [
            {'entry_id': '2026-11-10-evening-owl-text-10', 'action': 'self_reply', 'media_id': '18090474101701825'},
            {'entry_id': 'legacy-post', 'action': 'post', 'media_id': '17882132640545881'},
        ]
        targets = I.published_targets(QUEUE, receipts)
        media_ids = [row['media_id'] for row in targets]
        self.assertEqual(media_ids, ['18083115968422426', '18090474101701825', '17882132640545881'])
        main = next(row for row in targets if row['media_id'] == '18083115968422426')
        self.assertEqual(main['queue_id'], '2026-11-10-evening-owl-text-10')
        self.assertEqual(main['topic_tag'], '育兒日常')
        self.assertEqual(main['utm_campaign'], 'threads_winter_20261221')

    def test_parse_metrics_shapes(self):
        payload = {
            'data': [
                {'name': 'views', 'total_value': {'value': 12}},
                {'name': 'likes', 'values': [{'value': 3}]},
                {'name': 'replies', 'value': 1},
                {'name': 'ignored', 'value': 9},
            ]
        }
        self.assertEqual(I.parse_metrics(payload), {'views': 12, 'likes': 3, 'replies': 1})

    def test_missing_scope_detection(self):
        err = I.ApiError('GET', '/123/insights', 403, json.dumps({
            'error': {'message': '(#10) Requires threads_manage_insights permission', 'code': 10, 'type': 'OAuthException'}
        }))
        self.assertTrue(I.missing_insights_scope(err))
        other = I.ApiError('GET', '/123/insights', 400, json.dumps({
            'error': {'message': 'Unsupported metric', 'code': 100}
        }))
        self.assertFalse(I.missing_insights_scope(other))


class InsightsRun(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'social' / 'receipts').mkdir(parents=True)
        (self.root / 'social' / 'queue.json').write_text(json.dumps(QUEUE), encoding='utf-8')
        (self.root / 'social' / 'receipts' / '2026-11.jsonl').write_text(
            json.dumps({'entry_id': 'legacy-post', 'action': 'post', 'media_id': '17882132640545881'}) + '\n',
            encoding='utf-8',
        )
        self.now = datetime(2026, 11, 15, 6, 37, tzinfo=I.TAIPEI)

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_jsonl_and_does_not_touch_queue(self):
        calls = []

        def api(method, path, params, token):
            self.assertNotIn(token, path)
            calls.append(path)
            if path == '/me':
                return {'id': '123', 'username': 'lovehibou'}
            if path == '/debug_token':
                return {'data': {'is_valid': True, 'scopes': ['threads_basic', 'threads_content_publish', 'threads_manage_insights']}}
            if path.endswith('/insights'):
                return {'data': [{'name': 'views', 'values': [{'value': 8}]}, {'name': 'likes', 'value': 2}]}
            raise AssertionError(path)

        I.Runner(root=self.root, env={'HIBOU_THREADS_TOKEN': 'fixture-token'}, api=api, clock=lambda: self.now, commit=False).run()
        path = self.root / 'social' / 'metrics' / '2026-11-15.jsonl'
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        by_id = {row['media_id']: row for row in rows}
        self.assertEqual(set(by_id), {'18083115968422426', '17882132640545881'})
        main = by_id['18083115968422426']
        self.assertEqual(main['metrics'], {'views': 8, 'likes': 2})
        self.assertEqual(main['fetched_at'], '2026-11-15T06:37:00+08:00')
        self.assertEqual(main['utm_campaign'], 'threads_winter_20261221')
        self.assertEqual(main['queue_id'], '2026-11-10-evening-owl-text-10')
        self.assertEqual(main['topic_tag'], '育兒日常')
        self.assertEqual(json.loads((self.root / 'social' / 'queue.json').read_text()), QUEUE)
        self.assertTrue(any(path == '/me' for path in calls))
        self.assertTrue(any(path.endswith('/insights') for path in calls))

    def test_missing_scope_writes_nothing_and_is_clean(self):
        def api(method, path, params, token):
            if path == '/me':
                return {'id': '123', 'username': 'lovehibou'}
            raise I.ApiError('GET', path, 403, json.dumps({
                'error': {'message': 'Requires threads_manage_insights', 'code': 10}
            }))

        runner = I.Runner(root=self.root, env={'HIBOU_THREADS_TOKEN': 'fixture-token'}, api=api, clock=lambda: self.now, commit=False)
        with self.assertRaises(I.MissingInsightsScope):
            runner.run()
        self.assertFalse((self.root / 'social' / 'metrics').exists())
        self.assertEqual(json.loads((self.root / 'social' / 'queue.json').read_text()), QUEUE)

    def test_main_exits_zero_when_scope_missing(self):
        def api(method, path, params, token):
            if path == '/me':
                return {'id': '123', 'username': 'lovehibou'}
            raise I.ApiError('GET', path, 403, json.dumps({
                'error': {'message': 'Requires threads_manage_insights', 'code': 10}
            }))

        with patch.object(I, 'Runner') as runner_cls:
            runner_cls.return_value.run.side_effect = I.MissingInsightsScope(I.INSIGHTS_SCOPE)
            self.assertEqual(I.main([]), 0)

    def test_token_is_not_present_in_api_error_text(self):
        err = I.ApiError('GET', '/me', 400, 'access_token=secret-token-value is bad')
        self.assertNotIn('secret-token-value', str(err))
        self.assertIn('REDACTED', str(err))


if __name__ == '__main__':
    unittest.main()
