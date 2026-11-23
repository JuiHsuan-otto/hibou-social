"""Facebook Page and Instagram publishing through the Meta Graph API.

The page token is passed only as a function argument. It is never printed,
logged, or copied into an error message. Callers decide whether a run is live;
this module only talks to the API.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

GRAPH = 'https://graph.facebook.com/v21.0'
POLL_EVERY, POLL_MAX = 5, 60
# Instagram content publishing allows 50 posts per rolling 24 hours.
IG_QUOTA_STOP = 40
RECENT_LIMIT = 25


class GraphError(Exception):
    def __init__(self, method, path, status, body):
        self.status, self.body = status, body
        # The path only: the token travels in the query or the form body.
        super().__init__(f'{method} {path} -> {status}: {body[:300]}')

    def _error(self):
        try:
            return json.loads(self.body).get('error') or {}
        except (ValueError, AttributeError):
            return {}

    @property
    def auth(self):
        err = self._error()
        message = str(err.get('message', self.body)).lower()
        return err.get('code') in (102, 190) or 'expired' in message or 'session has expired' in message

    @property
    def permission(self):
        err = self._error()
        message = str(err.get('message', self.body)).lower()
        return err.get('code') in (10, 200, 299) or 'permission' in message or 'requires' in message


class PageMismatch(Exception):
    def __init__(self, expected, actual):
        self.expected, self.actual = expected, actual
        super().__init__(f'page {actual} != {expected}')


class PostError(Exception):
    """One Facebook or Instagram post could not be created."""


def _scrub(body, token):
    text = body if isinstance(body, str) else ''
    if token and text:
        text = text.replace(token, '[redacted]')
    return text


def graph(method, path, params, token):
    params = dict(params or {})
    params['access_token'] = token
    url, data = GRAPH + path, None
    if method == 'GET':
        url += '?' + urllib.parse.urlencode(params)
    else:
        data = urllib.parse.urlencode(params).encode()
    request = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as e:
        body = _scrub(e.read().decode('utf-8', 'replace'), token)
        raise GraphError(method, path, e.code, body) from None
    except urllib.error.URLError as e:
        raise GraphError(method, path, 0, _scrub(str(e.reason), token)) from None


class Client:
    def __init__(self, token, ig_user_id='', expected_page_id='', api=graph, sleep=None, head=None):
        self.token = token
        self.ig_user_id = (ig_user_id or '').strip()
        self.expected_page_id = (expected_page_id or '').strip()
        self.api = api
        self.sleep = sleep or (lambda _seconds: None)
        self.head = head
        self.page_id = None
        self.page_name = None
        self.ig_username = None

    def call(self, method, path, params=None):
        return self.api(method, path, params or {}, self.token)

    def load_page(self):
        me = self.call('GET', '/me', {'fields': 'id,name'})
        page_id = str(me.get('id') or '')
        if not page_id:
            raise GraphError('GET', '/me', 0, 'no page id')
        if self.expected_page_id and page_id != self.expected_page_id:
            raise PageMismatch(self.expected_page_id, page_id)
        self.page_id = page_id
        self.page_name = me.get('name') or ''
        return page_id, self.page_name

    def load_instagram(self):
        if not self.ig_user_id:
            raise GraphError('GET', '/instagram', 0, 'no ig user id')
        me = self.call('GET', f'/{self.ig_user_id}', {'fields': 'id,username'})
        found = str(me.get('id') or '')
        if found != self.ig_user_id:
            raise GraphError('GET', f'/{self.ig_user_id}', 0, 'instagram id mismatch')
        self.ig_username = me.get('username') or ''
        return self.ig_username, found

    def ig_quota_ok(self):
        """False when the rolling publishing quota is too close to the limit.

        A missing or unexpected payload does not block the run: the per-post
        call will still fail cleanly if Meta refuses.
        """
        try:
            payload = self.call('GET', f'/{self.ig_user_id}/content_publishing_limit',
                                {'fields': 'quota_usage,config'})
        except GraphError:
            return True
        row = payload
        if isinstance(payload, dict) and 'data' in payload:
            row = (payload.get('data') or [{}])[0]
        usage = row.get('quota_usage') if isinstance(row, dict) else None
        if usage is None:
            return True
        try:
            return int(usage) < IG_QUOTA_STOP
        except (TypeError, ValueError):
            return True

    def recent_facebook(self):
        payload = self.call('GET', f'/{self.page_id}/feed', {
            'fields': 'id,message,permalink_url,created_time', 'limit': RECENT_LIMIT,
        })
        return payload.get('data') or []

    def recent_instagram(self):
        payload = self.call('GET', f'/{self.ig_user_id}/media', {
            'fields': 'id,caption,permalink,timestamp', 'limit': RECENT_LIMIT,
        })
        return payload.get('data') or []

    def wait_instagram(self, container):
        waited = 0
        while waited < POLL_MAX:
            self.sleep(POLL_EVERY)
            waited += POLL_EVERY
            try:
                status = self.call('GET', f'/{container}', {'fields': 'status_code'})
            except GraphError:
                status = {}
            state = status.get('status_code')
            if state == 'FINISHED':
                return
            if state in ('ERROR', 'EXPIRED'):
                raise PostError(f'container {state}')
        raise PostError(f'container not FINISHED after {POLL_MAX}s')

    def publish_facebook(self, entry):
        created = self.call('POST', f'/{self.page_id}/feed', {
            'message': entry['text'],
            'link': entry['link'],
        })
        post_id = created.get('id')
        if not post_id:
            raise PostError('Facebook returned no post id')
        permalink = None
        try:
            permalink = self.call('GET', f'/{post_id}', {'fields': 'permalink_url'}).get('permalink_url')
        except GraphError:
            permalink = None
        return None, post_id, permalink

    def _ig_container(self, params):
        container = self.call('POST', f'/{self.ig_user_id}/media', params).get('id')
        if not container:
            raise PostError('Instagram returned no container id')
        self.wait_instagram(container)
        return container

    def _ig_publish(self, container):
        media_id = self.call('POST', f'/{self.ig_user_id}/media_publish', {'creation_id': container}).get('id')
        if not media_id:
            raise PostError(f'Instagram returned no media id for container {container}')
        permalink = None
        try:
            permalink = self.call('GET', f'/{media_id}', {'fields': 'permalink'}).get('permalink')
        except GraphError:
            permalink = None
        return container, media_id, permalink

    def publish_instagram(self, entry):
        if entry.get('media_type') == 'CAROUSEL':
            children = []
            for url in entry['image_urls']:
                children.append(self._ig_container({
                    'image_url': url,
                    'is_carousel_item': 'true',
                    'media_type': 'IMAGE',
                }))
            container = self._ig_container({
                'media_type': 'CAROUSEL',
                'children': ','.join(children),
                'caption': entry['text'],
            })
            return self._ig_publish(container)
        container = self._ig_container({
            'image_url': entry['image_url'],
            'caption': entry['text'],
            'media_type': 'IMAGE',
        })
        return self._ig_publish(container)
