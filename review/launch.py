"""Запускает обучение, экзамен или основной пул, созданные create_pools.py (номера берёт из pool_ids.json).

  python3 launch.py training     — открыть обучение
  python3 launch.py exam         — открыть экзамен (обучение исполнители видят вместе с ним)
  python3 launch.py main         — открыть основной пул, когда экзамен сдали хотя бы 10 человек

Перед запуском пула спрашиваем у платформы, можно ли его открыть. Новый пул обычно сначала должен пройти
модерацию: тогда скрипт отправляет его на модерацию, а открыть пул можно после одобрения, тем же скриптом или
кнопкой в интерфейсе. Нужна переменная окружения YA_TASKS_TOKEN_FILE — путь к файлу с OAuth-токеном.
"""
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
TOKEN = Path(os.environ["YA_TASKS_TOKEN_FILE"]).expanduser().read_text().strip()
API, NEW_API = "https://tasks.yandex.ru/api/v1", "https://tasks.yandex.ru/api/new/requester"


def api(method, url, payload=None, **q):
    req = Request(url + ("?" + urlencode(q) if q else ""), method=method,
                  data=json.dumps(payload).encode() if payload is not None else None,
                  headers={"Authorization": "OAuth " + TOKEN, "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"null")
    except HTTPError as e:
        raise SystemExit(f"{method} {url}: HTTP {e.code}\n" + e.read().decode(errors="replace").replace(TOKEN, "[TOKEN]")[:1500])


def wait_open(path):
    for _ in range(30):
        status = api("GET", API + path)["status"]
        if status == "OPEN":
            return status
        time.sleep(2)
    return status


what = sys.argv[1] if len(sys.argv) > 1 else ""
ids = json.loads((HERE / "pool_ids.json").read_text())
if what == "training":
    api("POST", f"{API}/trainings/{ids['training']}/open")
    print("обучение:", wait_open(f"/trainings/{ids['training']}"))
elif what in ("exam", "main"):
    pid = ids[what]
    if what == "main":
        passed = [u for u in api("GET", f"{API}/user-skills", skill_id=ids["skill"], limit=100)["items"]
                  if u.get("exact_value", u.get("value", 0)) >= 90]
        print("сдали экзамен:", len(passed))
        if len(passed) < 10 and "--force" not in sys.argv:
            raise SystemExit("меньше 10 человек: работа ляжет на слишком немногих (запустить всё равно: --force)")
    checks = api("GET", f"{NEW_API}/pools/{pid}/validate")
    blockers = [c for c in (checks if isinstance(checks, list) else []) if c.get("active") and c.get("blocker")]
    if any("MODERATION" in json.dumps(c) for c in blockers):
        api("PUT", f"{NEW_API}/poolModeration/{pid}", {"status": "READY"})
        print("пул отправлен на модерацию; после одобрения запустите скрипт ещё раз")
    elif blockers:
        raise SystemExit("платформа не даёт открыть пул: " + json.dumps(blockers, ensure_ascii=False))
    else:
        api("POST", f"{API}/pools/{pid}/open")
        print(what, wait_open(f"/pools/{pid}"))
else:
    raise SystemExit(__doc__)
