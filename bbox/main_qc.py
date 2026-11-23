"""Hidden BBOX controls and optional acceptance for pool 6870160."""
import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError

from score import iou, match_count, parse_boxes
from sync_exam import Api, PROJECT, completed_assignments, review_assignment

BASE = Path(__file__).resolve().parent
POOL = '6870160'
PLAN = BASE / 'private' / 'main-pages.json'
GOLD = BASE / 'private' / 'main-gold.json'
LIMIT = 0.60


def read_plan():
    plan = json.loads(PLAN.read_text())
    pages, controls = plan['pages'], plan['controls']
    counts = Counter(url for page in pages for url in page)
    hidden = set(controls.values())
    if (plan['pool'] != POOL or len(pages) != 44 or len(hidden) != len(controls) or len(hidden) != 10
            or any(len(p) != 10 or len(set(p)) != 10 or len(set(p) & hidden) > 1 for p in pages)
            or any(counts[u] != 4 for u in hidden)
            or sum(n == 1 for n in counts.values()) != 400):
        raise ValueError('Состав страниц не совпадает с планом 400 + 40.')
    return pages, controls


def gold_row(row, controls):
    photo_id, url = str(row['photo_id']), row['image_url']
    if controls.get(photo_id) != url or row.get('split') != 'control' or row.get('error') is not None:
        raise ValueError(f'Неожиданный контроль {photo_id}')
    raw = row['boxes']
    if not isinstance(raw, list) or row.get('status') != ('found' if raw else 'none'):
        raise ValueError(f'Проверьте рамки и статус {photo_id}')
    boxes = parse_boxes([dict(b, shape='rectangle') for b in raw])
    return url, boxes


def load_gold(rows, controls):
    gold = {}
    for row in rows:
        u, boxes = gold_row(row, controls)
        if u in gold:
            raise ValueError('Повтор контрольного фото в эталонах')
        gold[u] = boxes
    if set(gold) != set(controls.values()):
        missing = [i for i, u in controls.items() if u not in gold]
        raise ValueError('Нет подтверждённых эталонов: ' + ', '.join(missing))
    return gold


def prepare(source, output):
    pages, controls = read_plan()
    rows = [json.loads(line) for line in source.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    gold = load_gold(rows, controls)
    with output.open('w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f, delimiter='\t', lineterminator='\n')
        writer.writerow(['INPUT:image', 'GOLDEN:status'])
        for p in pages:
            for url in p:
                status = '' if url not in gold else ('found' if gold[url] else 'none')
                writer.writerow([url, status])
    tmp = GOLD.with_suffix('.tmp')
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    tmp.replace(GOLD)
    print('44 страницы по 10. Для загрузки: «Указать вручную», 10 заданий на странице.')


def control_quality(output, reference):
    status = output.get('status')
    if status not in ('found', 'none'):
        return None
    try:
        boxes = parse_boxes(output.get('result'))
    except (TypeError, ValueError):
        return None
    if (status == 'found') != bool(boxes):
        return None
    if len(boxes) != len(reference):
        return 0.0
    if not boxes:
        return 1.0
    # Largest cutoff admitting one distinct prediction for every reference.
    scores = sorted({iou(a, b) for a in boxes for b in reference}, reverse=True)
    return next(s for s in scores if match_count(boxes, reference, s) == len(boxes))


def controls_in_order(assignments, pages, gold):
    by_page = {frozenset(p): p for p in pages}
    events = defaultdict(list)
    for a in assignments:
        worker, aid = a['user_id'], a['id']
        submitted = datetime.fromisoformat(a['submitted'].replace('Z', '+00:00'))
        if submitted.tzinfo is None:
            submitted = submitted.replace(tzinfo=timezone.utc)
        tasks = a['tasks']
        urls = [t['input_values']['image'] for t in tasks]
        if len(urls) != 10 or frozenset(urls) not in by_page:
            raise ValueError(f'Страница {aid} отсутствует в плане. Проверка остановлена.')
        idx = next((i for i, u in enumerate(urls) if u in gold), None)
        if idx is None:
            continue
        sol = a.get('solutions')
        quality = None
        if isinstance(sol, list) and len(sol) == len(tasks):
            result = sol[idx]
            if isinstance(result, dict) and isinstance(result.get('output_values'), dict):
                quality = control_quality(result['output_values'], gold[urls[idx]])
        events[worker].append((submitted, aid, urls[idx], quality))
    return {w: sorted(e) for w, e in events.items()}


def passes_control(quality):
    return quality is not None and quality >= LIMIT - 1e-12


def bad_pair(events):
    previous = None
    for _, aid, _, quality in events:
        if quality is None or passes_control(quality):
            previous = None
        elif previous:
            return [previous, aid]
        else:
            previous = aid
    return None


def apply_ban(api, db, worker, pair):
    with db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM bans WHERE worker=?', (worker,)).fetchone():
            return
        restriction = dict(scope='POOL', pool_id=POOL, user_id=worker)
        note = f'Два контроля подряд с IoU ниже 0,60. Страницы {pair[0]}, {pair[1]}.'
        # Reconcile a successful remote write after a timeout or a local crash.
        result = api.request('GET', 'user-restrictions', params=restriction)
        exists = any(all(r.get(k) == v for k, v in restriction.items()) and not r.get('will_expire')
                     for r in result['items'])
        if not exists:
            reply = api.request('PUT', 'user-restrictions', body=dict(restriction, private_comment=note))
            if not isinstance(reply, dict) or not all(reply.get(k) == v for k, v in restriction.items()):
                raise RuntimeError('Яндекс не подтвердил блокировку пула. Проверьте результат.')
        db.execute('INSERT INTO bans VALUES(?,?,?)', (worker, *pair))


def accept_passing_pages(api, db, assignments, events):
    qualified = {worker for worker, history in events.items()
                 if not bad_pair(history) and any(passes_control(e[3]) for e in history)}
    if db is not None:
        qualified.difference_update(worker for worker, in db.execute('SELECT worker FROM bans'))
    for page in assignments:
        if page['status'] != 'SUBMITTED':
            continue
        aid = page['id']
        if page['user_id'] not in qualified:
            print(f'{aid}: остаётся на проверке по истории исполнителя.')
            continue
        if db is None:
            print(f'{aid}: подходит для приёмки по истории исполнителя.')
            continue
        status = review_assignment(api, db, POOL, aid, page['user_id'], 'ACCEPTED',
                                   'Страница принята по результатам контроля качества исполнителя.')
        print(f'{aid}: {status} по истории исполнителя.', flush=True)


def run_check(apply, accept_passing=False):
    pages, controls = read_plan()
    gold = load_gold(json.loads(GOLD.read_text()), controls)
    api = Api(os.environ['YANDEX_TASKS_TOKEN'])
    pool = api.request('GET', 'pools/' + POOL)
    if pool['id'] != POOL or pool['project_id'] != PROJECT:
        raise ValueError('Неверный проект или пул.')
    if accept_passing and (pool.get('auto_accept_solutions') is not False
                           or pool.get('reward_per_assignment') != 6):
        raise ValueError('Для автоприёмки ожидается отложенная приёмка и ставка 6 ₽.')
    assignments = completed_assignments(api, POOL)
    events = controls_in_order(assignments, pages, gold)
    db = sqlite3.connect(BASE / 'private' / 'main-bans.sqlite3', timeout=60) if apply else None
    try:
        if db is not None:
            db.execute('CREATE TABLE IF NOT EXISTS bans(worker TEXT PRIMARY KEY, first TEXT, second TEXT)')
        for worker, history in events.items():
            pair = bad_pair(history)
            if db is not None and pair:
                apply_ban(api, db, worker, pair)
            print(json.dumps({'worker': worker, 'controls': [[aid, q] for _, aid, _, q in history],
                              'below_threshold_twice': pair}, ensure_ascii=False))
        if accept_passing:
            accept_passing_pages(api, db, assignments, events)
    finally:
        if db is not None:
            db.close()
    print(f'Проверено контролей: {sum(map(len, events.values()))}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prepare_cmd = sub.add_parser('prepare')
    prepare_cmd.add_argument('source', type=Path)
    prepare_cmd.add_argument('output', type=Path)
    check_cmd = sub.add_parser('check')
    check_cmd.add_argument('--apply', action='store_true')
    check_cmd.add_argument('--accept-passing', action='store_true')
    check_cmd.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            prepare(args.source, args.output)
        else:
            while True:
                try:
                    run_check(args.apply, args.accept_passing)
                except HTTPError as exc:
                    if not args.watch or (exc.code < 500 and exc.code != 429):
                        raise
                    print(f'Яндекс вернул HTTP {exc.code}. Повтор через минуту.', file=sys.stderr, flush=True)
                except (URLError, TimeoutError, ConnectionError) as exc:
                    if not args.watch:
                        raise
                    print(f'Ошибка соединения: {exc}. Повтор через минуту.', file=sys.stderr, flush=True)
                if not args.watch:
                    break
                time.sleep(60)
    except KeyboardInterrupt:
        pass
    except (ValueError, KeyError, OSError, RuntimeError, sqlite3.Error) as exc:
        sys.exit(f'Проверка основного пула: {exc}')
