"""Classification, guardrails, dedupe and pacing for @lovehibou engagement.

Pure functions only: no network, no token, no clock reads. engage.py applies them.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta

import validate as V

GIFT_DATE_TEXT = '有送禮日的話，先用 LINE 問我們'

# Phase B keyword search. Seven queries × six daily runs stays under the
# Threads cap of 500 keyword queries per rolling 7 days.
KEYWORDS = (
    '新手爸媽',
    '懷孕',
    '彌月',
    '育兒日常',
    '寶寶好物',
    '媽媽心情',
    '親子生活',
)

REPLY_RUN_CAP = 12
COMMENT_RUN_CAP = 8
COMMENT_DAY_CAP = 40
COMMENT_AUTHOR_DAY_CAP = 2
MIN_GAP_SECONDS = 60
LOOKBACK_DAYS = 14
QUIET_MINUTES = 5
QUIET_ANCHORS = ((7, 30), (12, 30), (18, 30), (21, 30))

# A live row with one of these actions means that source was already handled.
HANDLED_ACTIONS = ('posted', 'skipped')

PHASE_A_SCOPES = ('threads_read_replies', 'threads_manage_replies', 'threads_content_publish')
MENTION_SCOPE = 'threads_manage_mentions'
SEARCH_SCOPE = 'threads_keyword_search'
KNOWN_SCOPES = PHASE_A_SCOPES + (MENTION_SCOPE, SEARCH_SCOPE, 'threads_basic', 'threads_manage_insights')

MAX_DRAFT_CHARS = 90
MAX_SENTENCES = 3
MAX_EMOJI = 2

# Wording that must never leave the account. Engagement copy also forbids URLs,
# prices, stock, lead times and any hashtag (topic tags belong on scheduled posts).
EFFICACY = (
    'UPF', '抗菌', '防蟎', '低敏', '可當雨衣', '睡眠用', '日本', '保證', '有機棉',
    'AI 製作', 'AI製作', '以 AI', '以AI', '參考原作', '參考 HiBOU 原作', '非親繪',
    '非 Tetsu 親繪', '非實景', '治療', '療效', '治癒', '預防疾病', '不會過敏',
    '抗過敏', '無毒', '醫療效果', '藥效', '安全無虞', '醫師推薦', '臨床證實',
    'ChatGPT', 'Gemini', 'Claude', '人工智慧', '語言模型', '自動回覆', '機器人回',
)
STOCK_OR_LEAD = (
    '現貨', '缺貨', '補貨', '庫存', '預購', '交期', '出貨', '到貨', '備貨', '製作天數',
)
JAPAN_CLAIMS = ('日本製', '日本設計', '日本熱銷')
SERVICE_PHRASES = (
    '感謝您的分享', '非常理解您的感受', '非常理解你的感受',
    '希望對您有幫助', '希望對你有幫助', '每個寶寶都是獨一無二的',
    '有任何問題歡迎', '很高興為您', '祝您',
)
SLOGANS = ('用心守護每一天', '給寶寶最好的', '品質第一')
SIMPLIFIED = ('宝宝', '妈妈', '这个', '没有', '什么', '谢谢')

SAFE_FALLBACK = '這段好真實，拍拍你。'

COMMERCE_TEXT = {
    'price': '這個想問的話，私訊或 LINE 跟我們說一聲就好。',
    'size': '尺寸想對一下的話，私訊或 LINE 跟我們說就好。',
    'buy': '想知道怎麼買，私訊或 LINE 跟我們說一聲就好。',
    'stock': '這個直接私訊或 LINE 問我們就好，我們回你。',
    'lead': '時間這種私訊或 LINE 問我們就好。',
    'embroidery': '刺繡想怎麼弄，私訊或 LINE 跟我們說就好。',
}
SENSITIVE_LINES = (
    '這件事我們想當面聽清楚，方便的話私訊跟我們說。',
    '看到你留這則，我們想好好處理，私訊我們一下。',
    '這種情況不適合在這裡講完，私訊跟我們說，我們看。',
)
GENUINE_LINES = (
    '「{detail}」讀完好有畫面，辛苦了。',
    '看到「{detail}」，新手時期真的會這樣。',
    '啊「{detail}」，拍拍你。',
    '「{detail}」好真實，笑完又有點心疼。',
)
COMMENT_LINES = (
    '「{detail}」這段好熟悉，新手時期每天都在學。',
    '看到「{detail}」，突然覺得被說中。',
    '「{detail}」寫得好真實，拍拍。',
    '啊「{detail}」，這也太日常了。',
)

ALWAYS_COMMERCE = (
    ('embroidery', ('刺繡', '繡名字', '客製', '訂製', '定制')),
    ('price', ('多少錢', '價格', '價錢', '價位', '報價', '售價', '貴不貴')),
    ('buy', ('怎麼買', '哪裡買', '如何買', '哪裡下單', '怎麼訂', '哪裡訂')),
    ('stock', ('有貨', '現貨', '缺貨', '補貨', '庫存', '還有賣')),
    ('lead', ('幾天到', '交期', '製作天數', '多久到', '什麼時候到')),
)
QUESTION_COMMERCE = (
    ('size', ('尺寸', '尺碼', '公分', '幾歲', '多大')),
    ('lead', ('出貨', '到貨')),
    ('buy', ('下單', '購買')),
    ('stock', ('有沒有貨',)),
)
QUESTION_CUES = (
    '嗎', '呢', '？', '?', '請問', '想問', '能不能', '可不可以', '有沒有',
    '怎麼', '如何', '多少', '哪裡', '哪裏', '是不是', '拜託',
)
GIFT_ALWAYS = ('送禮日', '趕得及', '來得及', '預產期', '指定日期', '指定日')
GIFT_TOPIC = ('禮物', '送禮', '彌月')
GIFT_WHEN = ('幾號', '哪天', '什麼時候', '何時', '日期', '趕不趕')

SPAM_WORDS = (
    '加賴', '日賺', '日入', '穩賺', '保證獲利', '投資群', '點擊連結', '免費領取',
    '賭場', '色情網', '代操', '跟單', '兼職日',
)
ABUSE_WORDS = (
    '幹你', '去死', '智障', '腦殘', '廢物', '王八蛋', '他媽', '垃圾帳號', '騙子',
)
GRIEF_WORDS = (
    '流產', '死產', '過世', '夭折', '安息', '小天使', '失去寶寶', '失去孩子', '告別式',
)
POLITICS_WORDS = (
    '民進黨', '國民黨', '民眾黨', '選舉', '總統', '立法委員', '統獨', '台獨',
    '政治', '柯文哲', '賴清德',
)
FLAME_WORDS = ('炎上', '筆戰', '戰翻', '垃圾人', '仇恨', '歧視', '出征')
SENSITIVE_WORDS = (
    '退貨', '退款', '客訴', '投訴', '申訴', '很爛', '品質很差', '破洞', '發霉', '瑕疵',
    '過敏', '紅疹', '窒息', '勒到', '危險', '不安全', '受傷', '流血', '很失望',
    '就醫', '看醫生', '發燒', '用藥', '藥物', '副作用', '症狀', '診斷', '治療', '食安', '中毒',
)
MEDICAL_HELP = (
    '看醫生', '就醫', '用藥', '劑量', '副作用', '急診', '黃疸', '疫苗', '診斷', '處方',
    '發燒', '症狀',
)
GIVEAWAY_WORDS = ('抽獎', '贈獎', '按讚抽', '留言抽', '分享抽', '免費送', '標記好友', '轉發抽')
PROMO_WORDS = ('折扣碼', '團購', '業配', '廣告合作', '下殺')
MINOR_PHOTO_TEXT = ('寶寶照片', '兒子照片', '女兒照片', '萌照', '睡臉照', '小朋友的照片')
VISUAL_MEDIA = ('IMAGE', 'VIDEO', 'CAROUSEL', 'CAROUSEL_ALBUM')

URL_RE = re.compile(r'https?://|www\.', re.I)
PRICE_RE = re.compile(
    r'(?:nt\$|ntd|twd|台幣|美金|售價|定價|特價)|(?:[$¥￥]\s*\d)|(?:\d[\d,]*\s*(?:元|塊))',
    re.I,
)
EMOJI_RE = re.compile(r'[\U0001F000-\U0001FAFF\u2600-\u27BF]')
CJK_RE = re.compile(r'[\u4e00-\u9fff]')
LIST_RE = re.compile(r'^(?:\d+[\.、\)]|[-*•・])\s*\S')
HASHTAG_MARK = ('#', '＃')
LINK_RE = re.compile(r'https?://\S+|www\.\S+', re.I)

REASON_LABEL = {
    'genuine': '一般回覆',
    'commerce': '改私訊',
    'gift_date': '送禮日',
    'sensitive': '需人工',
    'spam': '垃圾或攻擊',
    'grief': '傷慟',
    'skip_topic': '敏感話題',
    'hidden': '已隱藏',
    'empty': '空白',
    'guardrail': '文案未過',
    'duplicate': '已處理過',
    'minor_photo': '未成年人照片',
    'politics': '政治',
    'controversy': '爭議',
    'medical': '醫療求助',
    'giveaway': '抽獎',
    'scam': '詐騙',
    'promo': '業配',
    'too_short': '太短',
    'not_chinese': '不是中文',
    'too_old': '太舊',
    'is_reply': '不是原帖',
    'own_account': '自己的貼文',
    'repost': '轉發',
    'no_username': '沒有帳號',
    'run_cap': '本輪上限',
    'day_cap': '單日上限',
    'author_cap': '同一人上限',
    'same_post': '同一則已留過',
    'rate_limit': '速率限制',
    'policy': '社群政策',
}


def salt(source_id):
    return int(hashlib.sha256(str(source_id).encode('utf-8')).hexdigest()[:8], 16)


def looks_like_question(text):
    return any(cue in text for cue in QUESTION_CUES)


def is_gift_date(text):
    if any(word in text for word in GIFT_ALWAYS):
        return True
    return any(word in text for word in GIFT_TOPIC) and any(word in text for word in GIFT_WHEN)


def commerce_subtype(text):
    question = looks_like_question(text)
    found = []
    for subtype, words in ALWAYS_COMMERCE:
        if any(word in text for word in words):
            found.append(subtype)
    if question:
        for subtype, words in QUESTION_COMMERCE:
            if any(word in text for word in words):
                found.append(subtype)
    order = ('embroidery', 'size', 'price', 'buy', 'lead', 'stock')
    for subtype in order:
        if subtype in found:
            return subtype
    return None


def classify_inbound(text):
    """Return {class, subtype} for a reply or mention on our own content."""
    raw = (text or '').strip()
    if not raw:
        return {'class': 'empty', 'subtype': None}
    if _is_spam(raw):
        return {'class': 'spam', 'subtype': None}
    if any(word in raw for word in GRIEF_WORDS):
        return {'class': 'grief', 'subtype': None}
    if any(word in raw for word in POLITICS_WORDS) or any(word in raw for word in FLAME_WORDS):
        return {'class': 'skip_topic', 'subtype': None}
    if any(word in raw for word in SENSITIVE_WORDS):
        return {'class': 'sensitive', 'subtype': None}
    if is_gift_date(raw):
        return {'class': 'gift_date', 'subtype': None}
    subtype = commerce_subtype(raw)
    if subtype:
        return {'class': 'commerce', 'subtype': subtype}
    return {'class': 'genuine', 'subtype': None}


def should_reply(kind):
    return kind in ('genuine', 'commerce', 'gift_date', 'sensitive')


def needs_human(kind):
    return kind in ('sensitive', 'grief')


def _is_spam(text):
    if any(word in text for word in SPAM_WORDS) or any(word in text for word in ABUSE_WORDS):
        return True
    return len(LINK_RE.findall(text)) >= 3


def is_hidden(status):
    visible = {'', 'NOT_HUSHED', 'VISIBLE', 'UNHIDDEN', 'NONE', 'NOT_HIDDEN'}
    if status is None:
        return False
    return str(status).strip().upper() not in visible


def discovery_skip_reason(post, now):
    """Why a keyword-search post must not receive a comment, or None.

    `timestamp` is an aware datetime. `is_reply` is a real boolean.
    """
    if not isinstance(post, dict):
        return 'empty'
    username = str(post.get('username') or '').strip().lstrip('@')
    if not username:
        return 'no_username'
    if username.lower() == 'lovehibou':
        return 'own_account'
    if post.get('is_reply') is True:
        return 'is_reply'
    media_type = str(post.get('media_type') or '').upper()
    if media_type == 'REPOST_FACADE':
        return 'repost'
    text = str(post.get('text') or '').strip()
    when = post.get('timestamp')
    if not isinstance(when, datetime):
        return 'too_old'
    age = now - when
    if age > timedelta(days=LOOKBACK_DAYS) or age < timedelta(0):
        return 'too_old'
    if media_type in VISUAL_MEDIA or any(word in text for word in MINOR_PHOTO_TEXT):
        return 'minor_photo'
    if any(word in text for word in POLITICS_WORDS):
        return 'politics'
    if any(word in text for word in FLAME_WORDS):
        return 'controversy'
    if any(word in text for word in MEDICAL_HELP):
        return 'medical'
    if any(word in text for word in SENSITIVE_WORDS):
        return 'sensitive'
    if any(word in text for word in GRIEF_WORDS):
        return 'grief'
    if any(word in text for word in GIVEAWAY_WORDS):
        return 'giveaway'
    if any(word in text for word in SPAM_WORDS) or any(word in text for word in ABUSE_WORDS):
        return 'scam'
    if any(word in text for word in PROMO_WORDS):
        return 'promo'
    if not CJK_RE.search(text):
        return 'not_chinese'
    if len(text) < 8:
        return 'too_short'
    return None


def sentence_count(text):
    parts = re.split(r'[。！？!?]+', (text or '').strip())
    return len([part for part in parts if part.strip()])


def emoji_count(text):
    return len(EMOJI_RE.findall(text or ''))


def looks_like_list(text):
    hits = 0
    for line in (text or '').splitlines():
        if LIST_RE.match(line.strip()):
            hits += 1
    return hits >= 2


def validate_draft(text):
    """Return a list of problems. An empty list means the draft may be posted."""
    problems = []
    raw = text if isinstance(text, str) else ''
    stripped = raw.strip()
    if not stripped:
        return ['empty']
    if stripped != raw:
        problems.append('needs trim')
    if len(stripped) > MAX_DRAFT_CHARS:
        problems.append(f'longer than {MAX_DRAFT_CHARS}')
    if V.text_length(stripped) > V.TEXT_LIMIT:
        problems.append('over threads limit')
    if not CJK_RE.search(stripped):
        problems.append('not chinese')
    if sentence_count(stripped) > MAX_SENTENCES:
        problems.append(f'more than {MAX_SENTENCES} sentences')
    if emoji_count(stripped) > MAX_EMOJI:
        problems.append(f'more than {MAX_EMOJI} emoji')
    if looks_like_list(stripped):
        problems.append('list')
    if URL_RE.search(stripped):
        problems.append('url')
    if PRICE_RE.search(stripped):
        problems.append('price')
    if any(mark in stripped for mark in HASHTAG_MARK):
        problems.append('hashtag')
    if '您' in stripped:
        problems.append('您')
    lowered = stripped.lower()
    for word in EFFICACY + STOCK_OR_LEAD + JAPAN_CLAIMS + SERVICE_PHRASES + SLOGANS + SIMPLIFIED:
        if word.lower() in lowered:
            problems.append(f'banned:{word}')
    return problems


def _clean_source(text):
    cleaned = LINK_RE.sub('', text or '')
    cleaned = re.sub(r'[#＃]\S+', '', cleaned)
    cleaned = EMOJI_RE.sub('', cleaned)
    for word in JAPAN_CLAIMS + ('日本',):
        cleaned = cleaned.replace(word, '')
    cleaned = PRICE_RE.sub('', cleaned)
    cleaned = re.sub(r'\s+', '', cleaned)
    return cleaned


def concrete_detail(text):
    """A short slice of the other person's words, with banned bits removed."""
    cleaned = _clean_source(text)
    match = re.search(r'\d+\s*(?:個月|個多月|歲|小時)', cleaned)
    if match:
        start = max(0, match.start() - 4)
        detail = cleaned[start:match.end() + 2]
    else:
        chunk = re.split(r'[。！？!?,，、\n]', cleaned)[0]
        detail = chunk[:12]
    detail = detail.strip('「」"\'')
    if not detail or not CJK_RE.search(detail) or validate_draft(f'「{detail}」好真實，拍拍。'):
        return '這段'
    return detail[:18]


def _line_from(templates, source_id, detail):
    return templates[salt(source_id) % len(templates)].format(detail=detail)


def fixed_text(kind, source_id, subtype=None):
    if kind == 'gift_date':
        return GIFT_DATE_TEXT
    if kind == 'commerce':
        return COMMERCE_TEXT.get(subtype or 'buy', COMMERCE_TEXT['buy'])
    if kind == 'sensitive':
        return SENSITIVE_LINES[salt(source_id) % len(SENSITIVE_LINES)]
    return None


def template_text(kind, source_text, source_id, subtype=None):
    fixed = fixed_text(kind, source_id, subtype)
    if fixed:
        return fixed
    detail = concrete_detail(source_text)
    templates = COMMENT_LINES if kind == 'comment' else GENUINE_LINES
    text = _line_from(templates, source_id, detail)
    if validate_draft(text):
        text = _line_from(templates, source_id, '這段')
    if validate_draft(text):
        return SAFE_FALLBACK
    return text


def draft_problems_or_text(text):
    problems = validate_draft(text)
    return (text.strip(), []) if not problems else (None, problems)


def quiet_resume_at(now):
    """Return the instant posting may resume, or None when now is outside the quiet windows."""
    if now.utcoffset() is None:
        raise ValueError('now must be timezone-aware')
    for hour, minute in QUIET_ANCHORS:
        anchor = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        start = anchor - timedelta(minutes=QUIET_MINUTES)
        end = anchor + timedelta(minutes=QUIET_MINUTES)
        if start <= now < end:
            return end
    return None


def gap_seconds(last_at, now, gap=MIN_GAP_SECONDS):
    if last_at is None:
        return 0
    remain = gap - (now - last_at).total_seconds()
    return max(0.0, remain)


def load_log_rows(paths):
    rows = []
    for path in paths:
        try:
            text = path.read_text(encoding='utf-8')
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def handled_source_ids(rows):
    found = set()
    for row in rows:
        if row.get('dry_run'):
            continue
        if row.get('action') not in HANDLED_ACTIONS:
            continue
        source_id = row.get('source_id')
        if source_id:
            found.add(str(source_id))
    return found


def _posted_comment(row, day=None, author=None, source_id=None):
    # dry_run previews count inside the current run so the caps still apply.
    # They are not written to the dedupe log, so the next run starts clean.
    if row.get('type') != 'comment' or row.get('action') not in ('posted', 'dry_run'):
        return False
    if day is not None and not str(row.get('ts') or '').startswith(day):
        return False
    if author is not None and str(row.get('other_account') or '').lstrip('@').lower() != author.lower():
        return False
    if source_id is not None and str(row.get('source_id') or '') != str(source_id):
        return False
    return True


def comment_block_reason(rows, *, day, author, source_id, run_count):
    """None when a discovery comment is still allowed."""
    if run_count >= COMMENT_RUN_CAP:
        return 'run_cap'
    if sum(1 for row in rows if _posted_comment(row, day=day)) >= COMMENT_DAY_CAP:
        return 'day_cap'
    if author and sum(1 for row in rows if _posted_comment(row, day=day, author=author)) >= COMMENT_AUTHOR_DAY_CAP:
        return 'author_cap'
    if source_id and any(_posted_comment(row, source_id=source_id) for row in rows):
        return 'same_post'
    if source_id and str(source_id) in handled_source_ids(rows):
        return 'same_post'
    return None


def parse_iso(value):
    """Parse an aware ISO timestamp. Threads sometimes omits the colon in the offset."""
    if isinstance(value, datetime):
        return value if value.utcoffset() is not None else None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    if re.search(r'[+-]\d{4}$', text):
        text = text[:-2] + ':' + text[-2:]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.utcoffset() is None:
        return None
    return parsed


def latest_outbound_at(rows):
    """Most recent time we published anything, including scheduled posts in the receipts log."""
    latest = None
    for row in rows:
        if not isinstance(row, dict) or row.get('dry_run'):
            continue
        if row.get('action') not in ('posted', 'post', 'self_reply'):
            continue
        when = parse_iso(row.get('ts'))
        if when is not None and (latest is None or when > latest):
            latest = when
    return latest


def keyword_order(now):
    if not KEYWORDS:
        return ()
    start = now.hour % len(KEYWORDS)
    return KEYWORDS[start:] + KEYWORDS[:start]


def build_prompt(kind, source_text, detail):
    role = '回覆' if kind != 'comment' else '留言'
    banned = '、'.join((
        '您', '感謝您的分享', '非常理解您的感受', '希望對您有幫助', '每個寶寶都是獨一無二的',
        '網址', '價格', '現貨', '交期', '日本製', '日本設計', '日本熱銷', 'hashtag',
        'AI', '抗菌', '防蟎', '低敏', '保證',
    ))
    source = (source_text or '').replace('\n', ' ')[:400]
    return (
        f'你是 HiBOU @lovehibou 的社群編輯，用台灣人平常講話的繁體中文寫一則 Threads {role}。\n'
        f'對方原文：{source}\n'
        f'請接住這個具體細節：「{detail}」。\n'
        '規則：1 到 2 句短句；最多 1 個 emoji；開頭要自然，不要每則都一樣；'
        '不要客服腔、不要條列、不要口號、不要推銷。'
        f'禁止出現：{banned}。\n'
        '只輸出留言本身，不要引號、不要前綴。'
    )
