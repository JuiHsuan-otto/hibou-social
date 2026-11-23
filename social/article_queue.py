"""Turn a live HiBOU article into draft Threads, Facebook and Instagram queue entries.

    python3 social/article_queue.py https://www.ihibou.com/articles/example
    python3 social/article_queue.py --file urls.txt
    python3 social/article_queue.py --write URL

The default prints the drafts and does not write queue.json. --write appends
them. Every URL must return HTTP 200, and the three drafts for one article
use different copy at different minutes. Nothing here is approved, so the
publish workflow will not send it until a person changes status to approved.

Facebook gets a link post. Instagram gets a caption that says 連結放在簡介
and stores the article URL in bio_link, which is never sent to Instagram.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate as V  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TAIPEI = timezone(timedelta(hours=8))
USER_AGENT = 'hibou-social-article/1.0'
# These minutes are inside the existing threads-publish windows:
#   07:05 morning  -> 07:45 run (90 minutes)
#   12:20 / 12:25 noon -> 12:45 run (90 minutes)
# None of them is 07:30, 12:30, 18:30 or 21:30.
# Facebook and Instagram article posts use the 12:20 article slot of the HiBOU
# social calendar (RULES.md §1: 12:20 育兒圖文, 2–3 a week, 3+ hours from the
# 16:30 / 20:30 videos and the 20:35 stories that Business Suite posts).
PLATFORM_SLOTS = (
    ('threads', 'morning', (7, 5)),
    ('facebook', 'noon', (12, 20)),
    ('instagram', 'noon', (12, 25)),
)
# Monday, Wednesday, Friday: at most three article days a week. Tuesday and
# Thursday 12:20 are left for the Business Suite parenting relay.
ARTICLE_WEEKDAYS = (0, 2, 4)
FORBIDDEN_MINUTES = {(7, 30), (12, 30), (18, 30), (21, 30)}
THREADS_DAY_CAP = 4
LOOKAHEAD_DAYS = 60
ACTIVE = ('draft', 'approved', 'publishing', 'published', 'posted_by_chrome')
META_TAG_RE = re.compile(r'<meta\s+([^>]+?)>', re.I)
ATTR_RE = re.compile(r'''([:\w-]+)\s*=\s*(['"])(.*?)\2''', re.I)
TITLE_RE = re.compile(r'<title>(.*?)</title>', re.I | re.S)
BRAND_RE = re.compile(r'^(?:hibou|喜福)(?:\s*(?:喜福|生活苑))?$', re.I)


class ArticleError(Exception):
    """This URL cannot become queue entries."""


def fetch_url(url, method='GET'):
    request = urllib.request.Request(url, method=method, headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = b'' if method == 'HEAD' else response.read()
            return response.status, response.geturl(), body, response.headers.get('Content-Type')
    except urllib.error.HTTPError as e:
        raw = b''
        if method != 'HEAD':
            try:
                raw = e.read()
            except Exception:
                raw = b''
        ctype = e.headers.get('Content-Type') if e.headers else None
        return e.code, url, raw, ctype
    except urllib.error.URLError:
        return 0, url, b'', None


def _attrs(raw):
    found = {}
    for key, _quote, value in ATTR_RE.findall(raw):
        found[key.lower()] = html.unescape(value).strip()
    return found


def meta_values(page, key):
    values = []
    for raw in META_TAG_RE.findall(page):
        attrs = _attrs(raw)
        name = attrs.get('property') or attrs.get('name') or ''
        if name.lower() == key.lower() and attrs.get('content'):
            values.append(attrs['content'])
    return values


def page_title(page):
    match = TITLE_RE.search(page or '')
    if not match:
        return ''
    return re.sub(r'\s+', ' ', html.unescape(match.group(1))).strip()


def hook_from_title(title):
    parts = []
    for part in re.split(r'[|｜]', title or ''):
        bit = re.sub(r'\s+', ' ', part).strip(' 　')
        bit = ''.join(ch for ch in bit if not V.is_emoji(ch))
        if not bit or BRAND_RE.match(bit):
            continue
        parts.append(bit)
    if not parts:
        return ''
    questions = [part for part in parts if ('？' in part or '?' in part) and len(part) <= 42]
    hook = questions[-1] if questions else parts[0]
    if len(hook) > 36:
        hook = re.split(r'[，,。]', hook)[0].strip()
    return hook[:36].strip(' 　')


def mentions_gift_date(text):
    return any(cue in (text or '') for cue in V.GIFT_DATE_CUES)


def build_copy(hook, url, gift):
    """Three posts, three wordings. The first lines are different on purpose."""
    if gift:
        threads = f'「{hook}」\n{V.GIFT_DATE_LINE}\n{url}'
        facebook = f'準備「{hook}」的話，時間先別自己猜。\n{V.GIFT_DATE_LINE}'
        instagram = f'「{hook}」在官網。\n{V.GIFT_DATE_LINE}\n連結放在簡介。'
    else:
        threads = f'「{hook}」這篇，寫給正在手忙腳亂的人。\n先看完再出門就好。\n{url}'
        facebook = f'「{hook}」放上喜福官網了。\n想看的人點開這篇，慢慢讀。'
        instagram = f'小鷹把「{hook}」寫進官網。\n想看完整的，連結放在簡介。'
    return {'threads': threads, 'facebook': facebook, 'instagram': instagram}


def parse_page(body, final_url):
    page = body.decode('utf-8', 'replace') if isinstance(body, bytes) else (body or '')
    title = (meta_values(page, 'og:title') or [page_title(page) or ''])[0]
    description = (meta_values(page, 'og:description') or meta_values(page, 'description') or [''])[0]
    images = []
    for image in meta_values(page, 'og:image'):
        absolute = urllib.parse.urljoin(final_url, image)
        if absolute not in images:
            images.append(absolute)
    return {
        'title': re.sub(r'\s+', ' ', title).strip(),
        'description': re.sub(r'\s+', ' ', description).strip(),
        'images': images,
    }


def image_is_live(result):
    status, final_url, _body, content_type = result
    if status != 200:
        return False
    parts = urllib.parse.urlsplit(final_url)
    if parts.netloc != V.SITE_HOST:
        return False
    if content_type and not content_type.split(';', 1)[0].strip().lower().startswith('image/'):
        return False
    return V.public_image_problem(final_url) is None


def load_article(url, fetch):
    canonical = V.canonical_article_url(url)
    if not canonical:
        raise ArticleError(f'只收 https://{V.SITE_HOST}/articles/ 或 /journals/ 的文章網址：{url}')
    status, final_url, body, _ctype = fetch(canonical, 'GET')
    final_canonical = V.canonical_article_url(final_url)
    if status != 200 or final_canonical != canonical:
        raise ArticleError(f'{canonical} 沒有活著的 200（狀態 {status}）')
    parsed = parse_page(body, final_url)
    hook = hook_from_title(parsed['title'])
    if not hook:
        raise ArticleError(f'{canonical} 讀不到標題')
    shaped = [image for image in parsed['images'] if not V.public_image_problem(image)]
    if not shaped:
        raise ArticleError(f'{canonical} 沒有可用的 og:image（要是官網上的 JPEG 或 PNG）')
    live_images = []
    for image in shaped:
        if not image_is_live(fetch(image, 'HEAD')):
            raise ArticleError(f'{image} 沒有回 200')
        live_images.append(image)
    copy = build_copy(hook, canonical, mentions_gift_date(parsed['title'] + '\n' + parsed['description']))
    return {
        'url': canonical,
        'path': V.article_path(canonical),
        'title': parsed['title'],
        'hook': hook,
        'images': live_images[:10],
        'copy': copy,
    }


def _on_day(entry, day):
    when = V.parse_publish_at(entry.get('publish_at'))
    return bool(when and when.date() == day)


def threads_load(queue, day):
    count = 0
    for entry in queue:
        if V.entry_platform(entry) != 'threads' or entry.get('slot') == 'test':
            continue
        if entry.get('status') not in ACTIVE:
            continue
        if _on_day(entry, day):
            count += 1
    return count


def taken_stamps(queue):
    return {entry.get('publish_at') for entry in queue if isinstance(entry, dict)}


def first_open_day(queue, now, reserved):
    now = now.astimezone(TAIPEI)
    start = now.date()
    cutoff = now.replace(hour=6, minute=30, second=0, microsecond=0)
    if now >= cutoff:
        start += timedelta(days=1)
    taken = taken_stamps(queue)
    for offset in range(LOOKAHEAD_DAYS):
        day = start + timedelta(days=offset)
        if day in reserved or day.weekday() not in ARTICLE_WEEKDAYS:
            continue
        if threads_load(queue, day) >= THREADS_DAY_CAP:
            continue
        stamps = [f'{day.isoformat()}T{hour:02d}:{minute:02d}:00+08:00' for _platform, _slot, (hour, minute) in PLATFORM_SLOTS]
        if any(stamp in taken for stamp in stamps):
            continue
        if any((hour, minute) in FORBIDDEN_MINUTES for _platform, _slot, (hour, minute) in PLATFORM_SLOTS):
            continue
        return day
    raise ArticleError(f'往後 {LOOKAHEAD_DAYS} 天都排不進三個平台')


def already_queued(queue, path, platform):
    source = f'article:{path}'
    for entry in queue:
        if not isinstance(entry, dict):
            continue
        if entry.get('source') == source and V.entry_platform(entry) == platform:
            return True
    return False


def make_entries(article, day):
    path = article['path']
    slug = path.rsplit('/', 1)[-1]
    images = article['images']
    entries = []
    for platform, slot, (hour, minute) in PLATFORM_SLOTS:
        if (hour, minute) in FORBIDDEN_MINUTES:
            raise ArticleError(f'{platform} 的時間撞上不發文的整點')
        entry = {
            'id': f'{day.isoformat()}-{platform}-{slug}',
            'platform': platform,
            'slot': slot,
            'publish_at': f'{day.isoformat()}T{hour:02d}:{minute:02d}:00+08:00',
            'status': 'draft',
            'text': article['copy'][platform],
            'image_url': None,
            'image_sha256': None,
            'image_urls': None,
            'video_url': None,
            'link': None,
            'bio_link': None,
            'topic_tag': None,
            'self_reply': None,
            'pin_after_post': False,
            'source': f'article:{path}',
        }
        if platform == 'threads':
            entry['media_type'] = 'TEXT'
            entry['link'] = article['url']
            entry['topic_tag'] = '育兒日常'
        elif platform == 'facebook':
            entry['media_type'] = 'LINK'
            entry['link'] = article['url']
        else:
            entry['bio_link'] = article['url']
            if len(images) == 1:
                entry['media_type'] = 'IMAGE'
                entry['image_url'] = images[0]
            else:
                entry['media_type'] = 'CAROUSEL'
                entry['image_urls'] = images
        entries.append(entry)
    minutes = [entry['publish_at'][11:16] for entry in entries]
    if len(set(minutes)) != len(entries):
        raise ArticleError('三個平台的時間沒有錯開')
    texts = [entry['text'] for entry in entries]
    if len(set(texts)) != len(entries):
        raise ArticleError('三個平台的文案重複了')
    return entries


def plan(urls, queue, now, fetch, root=None, busy=()):
    """Return (new entries, messages). Raises nothing for a single bad URL.

    `busy` holds dates that already have a Facebook/Instagram post at 12:20 elsewhere
    (for example the Business Suite parenting relay); no article is put on them.
    """
    entries, messages, reserved = [], [], set(busy or ())
    seen = set()
    for raw in urls:
        url = (raw or '').strip()
        if not url or url.startswith('#'):
            continue
        if url in seen:
            messages.append(f'略過重複網址 {url}')
            continue
        seen.add(url)
        try:
            article = load_article(url, fetch)
            pending = [platform for platform, _slot, _clock in PLATFORM_SLOTS
                       if not already_queued(queue, article['path'], platform)]
            if not pending:
                messages.append(f'已經在佇列裡：{article["url"]}')
                continue
            if len(pending) != len(PLATFORM_SLOTS):
                raise ArticleError(f'{article["url"]} 已經有部分平台在佇列，這次不補半套')
            day = first_open_day(queue + entries, now, reserved)
            built = make_entries(article, day)
            problems = V.validate_queue(queue + entries + built, root or REPO_ROOT)
            fresh = [item for item in problems if item[0] in {entry['id'] for entry in built}]
            if fresh:
                detail = '；'.join(f'{entry_id}: {problem}' for entry_id, problem in fresh[:6])
                raise ArticleError(f'文案沒過檢查：{detail}')
            entries.extend(built)
            reserved.add(day)
            messages.append(f'{article["url"]} → {day.isoformat()} 三則草稿（{article["hook"]}）')
        except ArticleError as exc:
            messages.append(f'跳過：{exc}')
    return entries, messages


def read_urls(args):
    urls = list(args.urls)
    if args.file:
        urls.extend(args.file.read_text(encoding='utf-8').splitlines())
    return urls


def main(argv=None, fetch=None, clock=None, root=None):
    parser = argparse.ArgumentParser(description='Draft Threads, Facebook and Instagram posts from a live article.')
    parser.add_argument('urls', nargs='*', help='article or journal URLs')
    parser.add_argument('--file', type=Path, help='text file with one URL per line')
    parser.add_argument('--root', type=Path, default=REPO_ROOT)
    parser.add_argument('--write', action='store_true', help='append drafts to social/queue.json')
    parser.add_argument('--busy-dates', default='', help='YYYY-MM-DD,... days that already have a 12:20 FB/IG post')
    args = parser.parse_args(argv)
    urls = read_urls(args)
    if not urls:
        print('::error::給一個文章網址，或用 --file', file=sys.stderr)
        return 1
    root = Path(root or args.root)
    queue_path = root / 'social' / 'queue.json'
    queue = json.loads(queue_path.read_text(encoding='utf-8'))
    now = clock() if clock else datetime.now(TAIPEI)
    try:
        busy = {datetime.strptime(d.strip(), '%Y-%m-%d').date() for d in args.busy_dates.split(',') if d.strip()}
    except ValueError:
        print('::error::--busy-dates 要寫成 YYYY-MM-DD,YYYY-MM-DD', file=sys.stderr)
        return 1
    entries, messages = plan(urls, queue, now, fetch or fetch_url, root, busy)
    for line in messages:
        print(line, file=sys.stderr)
    print(json.dumps(entries, ensure_ascii=False, indent=2))
    failed = any(line.startswith('跳過：') for line in messages)
    if not args.write:
        print(f'{len(entries)} entries would be added (print only; add --write to save)', file=sys.stderr)
        return 1 if failed else 0
    if failed:
        print('queue.json not written', file=sys.stderr)
        return 1
    if not entries:
        return 0
    updated = queue + entries
    problems = V.validate_queue(updated, root)
    if problems:
        for entry_id, problem in problems:
            print(f'::error::{entry_id}: {problem}', file=sys.stderr)
        print('queue.json not written', file=sys.stderr)
        return 1
    queue_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'{len(entries)} entries added to social/queue.json', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
