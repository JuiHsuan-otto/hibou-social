"""Validate social/queue.json before anything is sent.

    python3 social/validate.py social/queue.json

CI runs this on every pull request and publish.py runs it before every send.
If a single draft or approved entry breaks a rule, the whole run sends nothing.
Only the standard library is used, so it runs on the Mac (Python 3.9) and on
the GitHub runner (Python 3.12) alike.
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
import sys
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_BASE = 'https://raw.githubusercontent.com/JuiHsuan-otto/hibou-social/main/'
IMAGE_PREFIX = RAW_BASE + 'social/images/'
SITE_HOST = 'www.ihibou.com'

SLOTS = ('morning', 'noon', 'evening', 'bedtime', 'test')
STATUSES = ('draft', 'approved', 'publishing', 'published', 'failed', 'skipped', 'posted_by_chrome')
MEDIA_TYPES = ('TEXT', 'IMAGE')
# Entries still waiting to go out get the full content check; finished ones only the structural one.
CONTENT_CHECKED = ('draft', 'approved')
REQUIRED = ('id', 'slot', 'publish_at', 'status', 'media_type', 'text')

TEXT_LIMIT = 500
LINK_LIMIT = 5
IMAGE_MAX_BYTES = 8 * 1024 * 1024
IMAGE_MIN_WIDTH, IMAGE_MAX_WIDTH = 320, 1440
IMAGE_MAX_RATIO = 10

# Wording that must never go out. Sources:
# - handoff spec hibou-threads-api-spec-20261024.md §6 rule 4
# - _review/threads-plan-20261013/TREND_PLAYBOOK.md 「寫法」（UPF、抗菌、防蟎、低敏、可當雨衣、睡眠用；商品名不寫日本）
# - AGENTS.md 動畫公開文案規則（不寫 AI 製作／參考原作／非親繪／非實景）
# - GOTS/OCS 有機棉證書 2020 已失效，對外不寫「有機棉」（只擋這個詞，「有機會」不受影響）
BANNED = (
    'UPF', '抗菌', '防蟎', '低敏', '可當雨衣', '睡眠用', '日本', '保證', '有機棉',
    'AI 製作', 'AI製作', '以 AI', '以AI', '參考原作', '參考 HiBOU 原作', '非親繪', '非 Tetsu 親繪', '非實景',
)

# Self-replies may only link to pages on the TREND_PLAYBOOK.md 「連結清單」.
SELF_REPLY_PATHS = (
    '/',
    '/products/hibou-windshield',
    '/articles/parenting-windshield-usage',
    '/articles/parenting-outing-bag-checklist',
    '/parenting-notes',
    '/products/hibou-custom',
    '/articles/parenting-baby-gift-guide',
    '/articles/parenting-maternity-list',
    '/articles/parenting-bib-guide',
    '/articles/parenting-baby-soothing',
    '/articles/parenting-gauze-washing',
    '/articles/parenting-rainy-laundry',
    '/articles/parenting-6gauze-guide',
    '/products/hibou-carrier',
)

URL_RE = re.compile(r'https?://[^\s<>"\'（）「」，。、！？]+')
ID_RE = re.compile(r'^[A-Za-z0-9._-]+$')
TRAILING_PUNCT = '.,;:!?)]}'
EMOJI_RANGES = (
    (0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2B00, 0x2BFF), (0x2300, 0x23FF),
    (0xFE0F, 0xFE0F), (0x200D, 0x200D), (0x3030, 0x3030), (0x303D, 0x303D),
    (0x3297, 0x3297), (0x3299, 0x3299), (0xE0020, 0xE007F),
)


def is_emoji(ch):
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in EMOJI_RANGES)


def text_length(text):
    """Threads counts emoji by their UTF-8 bytes; everything else counts as one."""
    return sum(len(ch.encode('utf-8')) if is_emoji(ch) else 1 for ch in text)


def find_urls(text):
    return [u.rstrip(TRAILING_PUNCT) for u in URL_RE.findall(text or '')]


def parse_publish_at(value):
    """Return an aware datetime for a '+08:00' timestamp, or None."""
    if not isinstance(value, str) or not value.endswith('+08:00'):
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.utcoffset() == timedelta(hours=8) else None


def image_info(data):
    """Return (format, width, height) for JPEG/PNG bytes, or None."""
    if data[:8] == b'\x89PNG\r\n\x1a\n' and data[12:16] == b'IHDR':
        width, height = struct.unpack('>II', data[16:24])
        return 'PNG', width, height
    if data[:2] != b'\xff\xd8':
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
            i += 1 if marker == 0xFF else 2
            continue
        length = struct.unpack('>H', data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height, width = struct.unpack('>HH', data[i + 5:i + 9])
            return 'JPEG', width, height
        i += 2 + length
    return None


def check_text(label, text, problems):
    if not isinstance(text, str) or not text.strip():
        problems.append(f'{label} is empty')
        return
    length = text_length(text)
    if length > TEXT_LIMIT:
        problems.append(f'{label} is {length} characters (limit {TEXT_LIMIT})')
    emoji = sorted({ch for ch in text if is_emoji(ch)})
    if emoji:
        problems.append(f'{label} contains emoji ' + ' '.join(f'U+{ord(c):04X}' for c in emoji) + ' (HiBOU Threads uses XD／哈哈 instead)')
    lowered = text.lower()
    for word in BANNED:
        if word.lower() in lowered:
            problems.append(f'{label} contains banned wording 「{word}」')
    urls = find_urls(text)
    if len(set(urls)) > LINK_LIMIT:
        problems.append(f'{label} has {len(set(urls))} distinct links (limit {LINK_LIMIT})')
    for url in urls:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != 'https' or parts.netloc != SITE_HOST:
            problems.append(f'{label} links outside https://{SITE_HOST}: {url}')
    return urls


def check_image(entry, root, problems):
    url = entry.get('image_url')
    if not isinstance(url, str) or not url.startswith(IMAGE_PREFIX):
        problems.append(f'image_url must start with {IMAGE_PREFIX}')
        return
    path = root / url[len(RAW_BASE):]
    if not path.is_file():
        problems.append(f'image file missing in repo: {url[len(RAW_BASE):]}')
        return
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != entry.get('image_sha256'):
        problems.append('image_sha256 does not match the file')
    if len(data) >= IMAGE_MAX_BYTES:
        problems.append(f'image is {len(data)} bytes (limit 8 MB)')
    info = image_info(data)
    if not info:
        problems.append('image is not a JPEG or PNG')
        return
    _, width, height = info
    if not IMAGE_MIN_WIDTH <= width <= IMAGE_MAX_WIDTH:
        problems.append(f'image width {width}px is outside {IMAGE_MIN_WIDTH}–{IMAGE_MAX_WIDTH}')
    if min(width, height) == 0 or max(width, height) / min(width, height) > IMAGE_MAX_RATIO:
        problems.append(f'image aspect ratio {width}x{height} exceeds {IMAGE_MAX_RATIO}:1')


def validate_entry(entry, root=REPO_ROOT):
    """Return a list of problems for one entry (empty when it is fine)."""
    problems = []
    if not isinstance(entry, dict):
        return ['entry is not an object']
    status = entry.get('status')
    if status not in STATUSES:
        problems.append(f'status {status!r} is not one of {", ".join(STATUSES)}')
    if not ID_RE.match(str(entry.get('id') or '')):
        problems.append('id is missing or has characters other than letters, digits, . _ -')
    if parse_publish_at(entry.get('publish_at')) is None:
        problems.append('publish_at must be an ISO time ending in +08:00')
    if status == 'posted_by_chrome' or status not in CONTENT_CHECKED:
        return problems

    for key in REQUIRED:
        if entry.get(key) in (None, ''):
            problems.append(f'missing {key}')
    if entry.get('slot') not in SLOTS:
        problems.append(f'slot {entry.get("slot")!r} is not one of {", ".join(SLOTS)}')
    media_type = entry.get('media_type')
    if media_type not in MEDIA_TYPES:
        problems.append(f'media_type {media_type!r} is not TEXT or IMAGE')
    check_text('text', entry.get('text'), problems)

    if media_type == 'IMAGE':
        check_image(entry, root, problems)
    elif entry.get('image_url'):
        problems.append('image_url is set but media_type is not IMAGE')

    link = entry.get('link')
    if link:
        if media_type != 'TEXT':
            problems.append('link can only be used with media_type TEXT')
        parts = urllib.parse.urlsplit(link)
        if parts.scheme != 'https' or parts.netloc != SITE_HOST:
            problems.append(f'link must be on https://{SITE_HOST}: {link}')

    tag = entry.get('topic_tag')
    if tag is not None:
        if not isinstance(tag, str) or not 1 <= len(tag) <= 50:
            problems.append('topic_tag must be 1–50 characters')
        elif '.' in tag or '&' in tag:
            problems.append('topic_tag cannot contain . or &')

    reply = entry.get('self_reply')
    if reply is not None:
        if not isinstance(reply, dict):
            problems.append('self_reply must be {"text": ...} or null')
        else:
            urls = check_text('self_reply.text', reply.get('text'), problems) or []
            for url in urls:
                path = urllib.parse.urlsplit(url).path.rstrip('/') or '/'
                if path not in SELF_REPLY_PATHS:
                    problems.append(f'self_reply link is not on the TREND_PLAYBOOK link list: {url}')

    if status == 'approved':
        for key in ('approved_by', 'approved_at'):
            if not entry.get(key):
                problems.append(f'approved entries need {key}')
        if entry.get('slot') == 'test' and entry.get('approved_by') != 'otto':
            problems.append('test posts need approved_by "otto"')
    return problems


def validate_queue(queue, root=REPO_ROOT):
    """Return [(entry id, problem)] for the whole queue."""
    if not isinstance(queue, list):
        return [('(queue)', 'queue.json must be a JSON array')]
    problems = []
    seen = set()
    for entry in queue:
        entry_id = entry.get('id') if isinstance(entry, dict) else None
        if entry_id in seen:
            problems.append((entry_id, 'duplicate id'))
        seen.add(entry_id)
        problems += [(entry_id or '(no id)', p) for p in validate_entry(entry, root)]
    return problems


def main(argv):
    path = Path(argv[0]) if argv else REPO_ROOT / 'social' / 'queue.json'
    try:
        queue = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        print(f'::error::cannot read {path}: {e}')
        return 1
    problems = validate_queue(queue, path.resolve().parent.parent)
    for entry_id, problem in problems:
        print(f'::error::{entry_id}: {problem}')
    print(f'{len(queue)} entries checked, {len(problems)} problems')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
