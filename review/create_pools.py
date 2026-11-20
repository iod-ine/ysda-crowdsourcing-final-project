"""Создаёт в Я.Заданиях обучение, экзамен и основной пул проверки разметки — с теми настройками, с которыми мы их запускали.

Собрано из скриптов, которые мы запускали по шагам; настройки сверены с пулами на платформе.
Всё создаётся закрытым, запускает launch.py. Проект с шаблоном и инструкцией (11943) уже должен существовать.

  python3 create_pools.py            — показать, что будет создано (ничего не меняет)
  python3 create_pools.py --apply    — создать; номера пулов сохраняются в pool_ids.json

Три контрольных фото оказались испорчены, и мы отключили их уже во время проверки (см. references.json,
поле disabled_during_run). Здесь они по умолчанию не загружаются; --with-disabled загрузит их, как было при запуске.

Нужны переменные окружения: YA_TASKS_TOKEN_FILE — путь к файлу с OAuth-токеном,
YA_TASKS_REQUESTER_ID (по желанию) — ID аккаунта заказчика, чтобы не создать пулы не в том аккаунте.
"""
import json
import os
import sys
from collections import Counter
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
IDS_FILE = HERE / "pool_ids.json"
PROJECT_ID = "11943"          # «Проверьте, правильно ли выделены бабочки»
BBOX_POOL = "6870160"         # основная разметка рамок: отсюда берём 400 фото с рамками

# обучение и экзамен
TRAINING_PASS = 83            # % верных в обучении, чтобы открылся экзамен (10 из 12)
EXAM_PASS = 90                # % верных на экзамене для допуска и оплаты (9 из 10)
EXAM_REWARD = 1.0             # ₽ за экзамен, платим только сдавшим
EXAM_ATTEMPTS = 30            # сколько человек может пройти экзамен

# основной пул
OVERLAP = 3                   # сколько человек проверяет каждое фото
REAL_PER_PAGE, CONTROLS_PER_PAGE = 8, 2
REWARD = 1.2                  # ₽ за страницу из 10 фото
CONTROL_PASS = 75             # % верных на контрольных: ниже — бан и отклонение страниц, выше — оплата
PAGE_LIMIT = 15               # страниц на человека
FAST_SECONDS = 15             # страница быстрее — подозрительно; две из последних пяти — бан на сутки

refs = json.loads((HERE / "references.json").read_text())["items"]
stage = lambda s: [r for r in refs if r["stage"] == s]
rect = lambda bs: [{"shape": "rectangle", **{k: float(b[k]) for k in ("left", "top", "width", "height")}} for b in bs]
known = lambda r: [{"output_values": {"verdict": r["verdict"]}, "correctness_weight": 1.0}]
TOKEN = Path(os.environ["YA_TASKS_TOKEN_FILE"]).expanduser().read_text().strip()


def api(method, path, payload=None, **q):
    req = Request("https://tasks.yandex.ru/api/v1" + path + ("?" + urlencode(q) if q else ""), method=method,
                  data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
                  headers={"Authorization": "OAuth " + TOKEN, "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=120) as r:
            return json.loads(r.read() or b"null")
    except HTTPError as e:
        raise SystemExit(f"{method} {path}: HTTP {e.code}\n" + e.read().decode(errors="replace").replace(TOKEN, "[TOKEN]")[:2000])


def list_all(path, **q):
    items = []
    while True:
        res = api("GET", path, limit=100, sort="id", **q)
        items += res["items"]
        if not res.get("has_more") or not res["items"]:
            return items
        q["id_gt"] = res["items"][-1]["id"]


def training_payload():
    return {"project_id": PROJECT_ID, "private_name": "Обучение проверке — 12 фото", "may_contain_adult_content": False,
            "inherited_instructions": True, "public_instructions": "", "assignment_max_duration_seconds": 1800,
            "mix_tasks_in_creation_order": False, "shuffle_tasks_in_task_suite": True,
            "training_tasks_in_task_suite_count": len(stage("training"))}


def training_tasks(training_id):
    # после неверного ответа исполнитель видит подсказку; правильные рамки передаются в шаблон
    return [{"pool_id": training_id, "infinite_overlap": True, "known_solutions": known(r), "message_on_unknown_solution": r["hint"],
             "input_values": {"image": r["image"], "boxes": rect(r["boxes"]), "photo_id": r["photo_id"], "labeler": r["id"],
                              "training": True, "training_hint": r["hint"], "reference_boxes": rect(r["reference_boxes"])}}
            for r in stage("training")]


def exam_payload(training_id, skill_id):
    n = len(stage("exam"))
    done = {"key": "golden_set_answers_count", "operator": "GTE", "value": n}
    rate = lambda op: {"key": "golden_set_correct_answers_rate", "operator": op, "value": EXAM_PASS}
    return {
        "project_id": PROJECT_ID, "private_name": "Экзамен проверки — 10 фото", "type": "REGULAR", "regular_subtype": "EXAM",
        "public_description": "Экзамен: 10 фото. Ответьте, правильно ли выделены бабочки.",
        "may_contain_adult_content": False, "will_expire": "2027-11-19T00:00:00",
        "reward_per_assignment": EXAM_REWARD, "assignment_max_duration_seconds": 900,
        "auto_accept_solutions": False, "auto_accept_period_day": 3, "auto_reject_enabled": True,
        "auto_reject_reason": "Экзамен не сдан. Если считаете это ошибкой, напишите заказчику — проверим.",
        "defaults": {"default_overlap_for_new_task_suites": EXAM_ATTEMPTS},
        "assignments_issuing_config": {"issue_task_suites_in_creation_order": False},
        "filter": {"and": [{"or": [{"category": "profile", "key": "languages", "operator": "IN", "value": "RU"}]}]},
        "quality_control": {
            "training_requirement": {"training_pool_id": training_id, "training_passing_skill_value": TRAINING_PASS},
            "configs": [{"collector_config": {"type": "GOLDEN_SET", "parameters": {"history_size": n}}, "rules": [
                # доля верных ответов становится навыком; по нему пускаем в основной пул
                {"conditions": [done], "action": {"type": "SET_SKILL_FROM_OUTPUT_FIELD",
                                                  "parameters": {"skill_id": skill_id, "from_field": "correct_answers_rate"}}},
                {"conditions": [done, rate("GTE")], "action": {"type": "APPROVE_ALL_ASSIGNMENTS", "parameters": {}}},
                {"conditions": [done, rate("LT")], "action": {"type": "REJECT_ALL_ASSIGNMENTS", "parameters": {
                    "public_comment": f"Экзамен не сдан: верных ответов меньше {EXAM_PASS * n // 100} из {n}."}}}]}]},
    }


def exam_suite(exam_id):
    return {"pool_id": exam_id, "overlap": EXAM_ATTEMPTS, "tasks": [
        {"known_solutions": known(r), "input_values": {"image": r["image"], "boxes": rect(r["boxes"]), "photo_id": r["photo_id"],
                                                       "labeler": r["id"]}} for r in stage("exam")]}


def main_payload(skill_id):
    rate = lambda op: {"key": "golden_set_correct_answers_rate", "operator": op, "value": CONTROL_PASS}
    cnt = lambda v: {"key": "golden_set_answers_count", "operator": "GTE", "value": v}
    ban = lambda comment, **extra: {"type": "RESTRICTION_V2", "parameters": {"scope": "POOL", "private_comment": comment, **extra}}
    return {
        "project_id": PROJECT_ID, "private_name": "Основная проверка bbox — 400 фото, перекрытие 3", "type": "REGULAR",
        "public_description": "Посмотрите на фотографии с рамками другого исполнителя и ответьте, правильно ли выделены бабочки. "
                              "Если рамка неточная, подправьте её. На странице 10 фото. "
                              "Страница оплачивается после автоматической проверки ответов на контрольных фото.",
        "may_contain_adult_content": False, "will_expire": "2027-11-19T00:00:00",
        "reward_per_assignment": REWARD, "assignment_max_duration_seconds": 1800,
        # автоприёмки нет: страницу принимает или отклоняет правило по контрольным, остальное отклоняется через сутки
        "auto_accept_solutions": False, "auto_accept_period_day": 1, "auto_reject_enabled": True,
        "auto_reject_reason": "Страница не прошла автоматическую проверку по контрольным фото.",
        "defaults": {"default_overlap_for_new_tasks": OVERLAP, "default_overlap_for_new_task_suites": OVERLAP},
        "mixer_config": {"real_tasks_count": REAL_PER_PAGE, "golden_tasks_count": CONTROLS_PER_PAGE, "training_tasks_count": 0,
                         "min_real_tasks_count": 1, "min_golden_tasks_count": 1, "min_training_tasks_count": 0,
                         "force_last_assignment": True, "force_last_assignment_delay_seconds": 600,
                         "mix_tasks_in_creation_order": False, "shuffle_tasks_in_task_suite": True},
        "filter": {"and": [{"or": [{"category": "profile", "key": "languages", "operator": "IN", "value": "RU"}]},
                           {"or": [{"category": "skill", "key": skill_id, "operator": "GTE", "value": EXAM_PASS}]}]},
        "quality_control": {"configs": [
            {"collector_config": {"type": "GOLDEN_SET", "parameters": {"history_size": 10}}, "rules": [
                {"conditions": [cnt(4), rate("LT")], "action": ban(
                    f"Меньше {CONTROL_PASS}% верных ответов на контрольных фото — ответы не учитываем", duration_unit="PERMANENT")},
                {"conditions": [cnt(4), rate("LT")], "action": {"type": "REJECT_ALL_ASSIGNMENTS", "parameters": {
                    "public_comment": f"Слишком много ошибок на контрольных фото: меньше {CONTROL_PASS}% верных ответов."}}},
                {"conditions": [cnt(2), rate("GTE")], "action": {"type": "APPROVE_ALL_ASSIGNMENTS", "parameters": {}}}]},
            {"collector_config": {"type": "ASSIGNMENT_SUBMIT_TIME",
                                  "parameters": {"history_size": 5, "fast_submit_threshold_seconds": FAST_SECONDS}},
             "rules": [{"conditions": [{"key": "fast_submitted_count", "operator": "GTE", "value": 2}], "action": ban(
                 f"Две страницы из последних пяти сданы быстрее {FAST_SECONDS} секунд", duration_unit="DAYS", duration=1)}]},
            {"collector_config": {"type": "ANSWER_COUNT"},
             "rules": [{"conditions": [{"key": "assignments_accepted_count", "operator": "GTE", "value": PAGE_LIMIT}], "action": ban(
                 f"Лимит: не больше {PAGE_LIMIT} страниц на человека", duration_unit="PERMANENT")}]},
            # отклонённую страницу отдаём другому человеку, чтобы у фото осталось три засчитанные проверки
            {"collector_config": {"type": "ASSIGNMENTS_ASSESSMENT"},
             "rules": [{"conditions": [{"key": "assessment_event", "operator": "EQ", "value": "REJECT"}],
                        "action": {"type": "CHANGE_OVERLAP", "parameters": {"delta": 1, "open_pool": True}}}]},
        ]},
    }


def bbox_answers():
    """400 ответов основной разметки рамок и их авторы (контрольные задания того пула пропускаем)."""
    suites = {s["id"]: s for s in list_all("/task-suites", pool_id=BBOX_POOL)}
    real, authors = [], set()
    for a in list_all("/assignments", pool_id=BBOX_POOL):
        assert a["status"] == "ACCEPTED", a["status"]
        authors.add(a["user_id"])
        for t, s in zip(suites[a["task_suite_id"]]["tasks"], a["solutions"]):
            if t.get("known_solutions"):
                continue
            img = t["input_values"]["image"]
            real.append({"image": img, "boxes": rect(s["output_values"].get("result") or []), "photo_id": img.split("/")[-2],
                         "labeler": a["user_id"]})
    assert len(real) == 400 and len({r["image"] for r in real}) == 400, len(real)
    return real, authors


def main_tasks(main_id, real):
    controls = [r for r in stage("control") if "disabled_during_run" not in r or "--with-disabled" in sys.argv]
    return [{"pool_id": main_id, "overlap": OVERLAP, "input_values": r} for r in real] + [
        {"pool_id": main_id, "infinite_overlap": True, "known_solutions": known(r),
         "input_values": {"image": r["image"], "boxes": rect(r["boxes"]), "photo_id": r["photo_id"], "labeler": f"control:{r['id']}"}}
        for r in controls]


if __name__ == "__main__":
    if os.environ.get("YA_TASKS_REQUESTER_ID"):
        assert api("GET", "/requester")["id"] == os.environ["YA_TASKS_REQUESTER_ID"], "токен не от того аккаунта"
    real, authors = bbox_answers()
    print(f"обучение: {len(stage('training'))} фото с подсказками, допуск к экзамену от {TRAINING_PASS}%")
    print(f"экзамен: {len(stage('exam'))} фото, {EXAM_REWARD} ₽ только сдавшим (от {EXAM_PASS}%), до {EXAM_ATTEMPTS} попыток")
    print(f"основной пул: {len(real)} фото × {OVERLAP} проверки, страница {REAL_PER_PAGE}+{CONTROLS_PER_PAGE} за {REWARD} ₽, "
          f"контрольных {len(main_tasks('x', real)) - len(real)}; ответы разметчиков: {dict(Counter(len(r['boxes']) for r in real))} рамок")
    print(f"авторов разметки рамок не пускаем в проект: {len(authors)}")
    if "--apply" not in sys.argv:
        raise SystemExit("ничего не создано: добавьте --apply")

    ids = json.loads(IDS_FILE.read_text()) if IDS_FILE.exists() else {}
    save = lambda: IDS_FILE.write_text(json.dumps(ids, indent=1))
    if "skill" not in ids:
        ids["skill"] = api("POST", "/skills", {"name": "Бабочки — экзамен проверки рамок", "hidden": True})["id"]; save()
    if "training" not in ids:
        ids["training"] = api("POST", "/trainings", training_payload())["id"]; save()
        api("POST", "/tasks", training_tasks(ids["training"]), allow_defaults="true", async_mode="false")
    if "exam" not in ids:
        ids["exam"] = api("POST", "/pools", exam_payload(ids["training"], ids["skill"]))["id"]; save()
        api("POST", "/task-suites", exam_suite(ids["exam"]), allow_defaults="true")
    if "main" not in ids:
        ids["main"] = api("POST", "/pools", main_payload(ids["skill"]))["id"]; save()
        tasks = main_tasks(ids["main"], real)
        for i in range(0, len(tasks), 100):
            api("POST", "/tasks", tasks[i:i + 100], allow_defaults="true", async_mode="false")
    if not ids.get("authors_blocked"):
        for u in authors:
            api("PUT", "/user-restrictions", {"scope": "PROJECT", "project_id": PROJECT_ID, "user_id": u,
                                             "private_comment": "Автор разметки рамок: не проверяет свою работу"})
        ids["authors_blocked"] = len(authors); save()
    print("создано (всё закрыто):", ids)
