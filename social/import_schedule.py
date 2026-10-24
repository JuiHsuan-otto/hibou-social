"""Turn the Mac's Threads schedule.json into queue.json entries.

    python3 social/import_schedule.py                 # print what would be added
    python3 social/import_schedule.py --write         # add them and copy the images

Run it in an attended session. schedule.json is only read, never changed.
- `planned` items inside the next --days days become approved bedtime entries;
  their images are copied to social/images/YYYY/MMDD-bedtime.jpg.
- `planned` items whose posting window has already passed become `skipped`
  (Otto 2026-10-24: a post whose time has passed is skipped, not re-sent).
- `posted` items become `posted_by_chrome`, so the record is complete.
Entries already in queue.json are left exactly as they are.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate as V  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
HIBOU = Path('/Volumes/Transcend-Dev/AI-Workspace/projects/business/hibou')
SCHEDULE = HIBOU / '_review/threads-plan-20261013/schedule.json'
TAIPEI = timezone(timedelta(hours=8))
BEDTIME_WINDOW = timedelta(hours=2)
APPROVER = 'claude (CLAUDE_AUTHORIZATION §6)'


def build(schedule, queue, root, source_root, today, days, now):
    """Return (new entries, image copies as (source, destination))."""
    known = {e.get('id') for e in queue}
    entries, copies = [], []
    last = today + timedelta(days=days - 1)
    for item in schedule['items']:
        day = date.fromisoformat(item['date'])
        entry_id = f"{item['date']}-bedtime"
        if entry_id in known:
            continue
        publish_at = f"{item['date']}T{item.get('time', '21:30')}:00+08:00"
        base = {
            'id': entry_id, 'slot': 'bedtime', 'publish_at': publish_at,
            'media_type': 'IMAGE' if item.get('image') else 'TEXT',
            'text': item['text'], 'image_url': None, 'image_sha256': None, 'link': None,
            'topic_tag': item.get('topic') or '育兒日常', 'self_reply': None,
            'pin_after_post': bool(item.get('pin_after_post')), 'source': f"schedule.json#{item['date']}",
        }
        status = item.get('status')
        if status == 'posted':
            base.update(status='posted_by_chrome', permalink=item.get('post_url'), published_at=item.get('posted_at'))
            entries.append(base)
            continue
        if status != 'planned':
            continue
        if V.parse_publish_at(publish_at) + BEDTIME_WINDOW < now:
            base.update(status='skipped', note='missed window: Chrome 時期沒有發出，時間已過不補發')
            entries.append(base)
            continue
        if day > last:
            continue
        if item.get('image'):
            source = source_root / item['image']
            data = source.read_bytes()
            relative = f"social/images/{item['date'][:4]}/{item['date'][5:7]}{item['date'][8:10]}-bedtime{source.suffix.lower()}"
            copies.append((source, root / relative))
            base.update(image_url=V.RAW_BASE + relative, image_sha256=hashlib.sha256(data).hexdigest())
        base.update(status='approved', approved_by=APPROVER, approved_at=now.isoformat(timespec='seconds'))
        entries.append(base)
    return entries, copies


def main(argv=None):
    parser = argparse.ArgumentParser(description='Import schedule.json bedtime posts into queue.json.')
    parser.add_argument('--schedule', type=Path, default=SCHEDULE)
    parser.add_argument('--source-root', type=Path, default=HIBOU, help='folder the schedule image paths are relative to')
    parser.add_argument('--root', type=Path, default=REPO_ROOT, help='hibou-social repo root')
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--from', dest='start', type=date.fromisoformat, help='first day to import (default: today, Taipei)')
    parser.add_argument('--write', action='store_true', help='add to queue.json and copy images (default: print only)')
    args = parser.parse_args(argv)

    now = datetime.now(TAIPEI)
    queue_path = args.root / 'social' / 'queue.json'
    queue = json.loads(queue_path.read_text(encoding='utf-8'))
    schedule = json.loads(args.schedule.read_text(encoding='utf-8'))
    entries, copies = build(schedule, queue, args.root, args.source_root, args.start or now.date(), args.days, now)

    print(json.dumps(entries, ensure_ascii=False, indent=2))
    for source, target in copies:
        print(f'image: {source} -> {target.relative_to(args.root)}', file=sys.stderr)
    if not args.write:
        print(f'{len(entries)} entries would be added (print only; add --write to save)', file=sys.stderr)
        return 0

    for source, target in copies:
        if target.exists() and target.read_bytes() != source.read_bytes():
            print(f'::error::{target} already exists with different content; nothing written', file=sys.stderr)
            return 1
    for source, target in copies:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(source, target)
    queue = sorted(queue + entries, key=lambda e: e.get('publish_at', ''))
    problems = V.validate_queue(queue, args.root)
    if problems:
        for entry_id, problem in problems:
            print(f'::error::{entry_id}: {problem}', file=sys.stderr)
        print('queue.json not written; the copied images are left for inspection', file=sys.stderr)
        return 1
    queue_path.write_text(json.dumps(queue, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'{len(entries)} entries added to {queue_path.relative_to(args.root)}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
