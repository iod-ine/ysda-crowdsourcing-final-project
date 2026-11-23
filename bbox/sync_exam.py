"""Score BBOX practice and exam pages, review paid exams, and grant qualifications."""

import argparse
import getpass
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

import score


PROJECT = "11824"
EXAM_POOL = "6870155"
LEGACY_EXAM_POOL = "6870068"
EXAM_SKILL = "21764"
TRAINING_POOL = "6870102"
TRAINING_SKILL = "21767"
TRAINING_NAME = "Бабочки — обучение по рамкам"
OLD_TRAINING_POOLS = {"6868736", "6868749"}
OLD_TRAINING_SKILLS = {"21600", "21607"}
SKILL_NAME = "Бабочки — экзамен по рамкам"
COMPLETED = {"SUBMITTED", "ACCEPTED", "REJECTED"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Api:
    def __init__(self, token):
        self.token = token
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, params=None, body=None):
        url = "https://tasks.yandex.ru/api/v1/" + path
        if params:
            url += "?" + urlencode(params)
        request = Request(url, method=method, headers={"Authorization": "OAuth " + self.token})
        if body is not None:
            request.add_header("Content-Type", "application/json")
            request.data = json.dumps(body).encode()
        with self.opener.open(request, timeout=30) as response:
            data = response.read()
        return json.loads(data) if data else None


def completed_assignments(api, pool_id):
    params = {"pool_id": pool_id, "status": ",".join(sorted(COMPLETED)),
              "sort": "id", "limit": 100}
    collected = []
    seen = set()
    while True:
        page = api.request("GET", "assignments", params=params)
        items = page["items"]
        if not isinstance(items, list) or type(page["has_more"]) is not bool:
            raise ValueError("Invalid assignments page")
        for item in items:
            if item["pool_id"] != pool_id or item["status"] not in COMPLETED:
                raise ValueError("Unexpected pool or assignment status")
            if not item["id"] or item["id"] in seen:
                raise ValueError("Missing or repeated assignment ID during pagination")
            seen.add(item["id"])
            collected.append(item)
        if not page["has_more"]:
            return collected
        if not items:
            raise ValueError("Incomplete assignments page without a cursor")
        params["id_gt"] = items[-1]["id"]


def current_skill(api, skill_id, worker_id):
    page = api.request("GET", "user-skills", params={"skill_id": skill_id, "user_id": worker_id})
    items = page["items"]
    if page["has_more"] is not False or not isinstance(items, list) or len(items) > 1:
        raise ValueError("Ambiguous skill lookup")
    if not items:
        return None
    item = items[0]
    if item["user_id"] != worker_id or item["skill_id"] != skill_id:
        raise ValueError("Skill lookup returned an unexpected ID")
    return item["value"]


def init_ledger(db):
    db.execute("""CREATE TABLE IF NOT EXISTS grants (
        pool_id TEXT NOT NULL, skill_id TEXT NOT NULL, assignment_id TEXT NOT NULL,
        worker_id TEXT NOT NULL, granted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (pool_id, skill_id, assignment_id))""")


def grant(api, db, pool_id, skill_id, attempt):
    key = (pool_id, skill_id, attempt["assignment_id"])
    worker = attempt["worker_id"]
    # Serializes two local instances through the same ledger, including the remote update.
    with db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT worker_id FROM grants WHERE pool_id=? AND skill_id=? AND assignment_id=?", key
        ).fetchone()
        if existing:
            if existing[0] != worker:
                raise ValueError("Worker differs from the recorded grant")
            return "recorded"
        value = current_skill(api, skill_id, worker)
        if value is not None and value != 100:
            return "existing_different_value"
        if value is None:
            api.request("PUT", "user-skills", body={"skill_id": skill_id, "user_id": worker, "value": 100})
            if current_skill(api, skill_id, worker) != 100:
                raise RuntimeError("Skill update has not been confirmed by the API")
        db.execute(
            "INSERT INTO grants (pool_id,skill_id,assignment_id,worker_id) VALUES (?,?,?,?)",
            (*key, worker),
        )
        return "granted" if value is None else "already_granted"


def exam_comment(attempt):
    if attempt["decision"] == "pass":
        return "Экзамен сдан. Страница принята к оплате."
    counts = attempt[f"IoU{score.EXAM_IOU}"]
    cutoff = f"{score.EXAM_IOU:.2f}".replace(".", ",")
    reasons = [
        f"При IoU ≥ {cutoff} совпало {counts['tp']} из {counts['tp'] + counts['fn']} "
        f"эталонных рамок. Для зачёта нужны все совпадения без лишних рамок. "
        f"Рамок исполнителя без совпадения: {counts['fp']}. "
        f"Эталонных рамок без совпадения: {counts['fn']}."
    ]
    if attempt["false_boxes_on_empty"]:
        reasons.append(f"Рамок на фото без бабочек: {attempt['false_boxes_on_empty']}. Допускается 0.")
    return "Экзамен не сдан. " + " ".join(reasons)


def review_status(assignment, pool_id, assignment_id, worker_id):
    if (not isinstance(assignment, dict) or assignment.get("pool_id") != pool_id
            or assignment.get("id") != assignment_id
            or assignment.get("user_id") != worker_id
            or assignment.get("status") not in COMPLETED):
        raise ValueError("Unexpected assignment in review")
    return assignment["status"]


def review_assignment(api, db, pool_id, assignment_id, worker_id, desired, comment):
    path = "assignments/" + assignment_id
    with db:
        # Other local checkers using this ledger must refresh the state after this review.
        db.execute("BEGIN IMMEDIATE")
        status = review_status(api.request("GET", path), pool_id, assignment_id, worker_id)
        if status != "SUBMITTED":
            return status
        try:
            reply = api.request("PATCH", path, body={"status": desired, "public_comment": comment})
        except HTTPError as exc:
            if exc.code != 409:
                raise
            reply = None
        if reply is None:
            reply = api.request("GET", path)
        status = review_status(reply, pool_id, assignment_id, worker_id)
        if status == "SUBMITTED":
            raise RuntimeError("Review has not been confirmed by the API")
        return status


def review_exam(api, db, attempt):
    desired = "ACCEPTED" if attempt["decision"] == "pass" else "REJECTED"
    return review_assignment(api, db, EXAM_POOL, attempt["assignment_id"], attempt["worker_id"],
                             desired, exam_comment(attempt))


def run_once(api, pool_id, skill_id, skill_name, gold, ledger=None, *, training=False):
    if (not pool_id.isdecimal() or not skill_id.isdecimal() or pool_id in OLD_TRAINING_POOLS
            or skill_id in OLD_TRAINING_SKILLS):
        raise ValueError("Expected a current pool and qualification skill")
    if training:
        if (pool_id, skill_id, skill_name) != (TRAINING_POOL, TRAINING_SKILL, TRAINING_NAME):
            raise ValueError("Unexpected training pool or training skill")
    elif pool_id == TRAINING_POOL or skill_id == TRAINING_SKILL:
        raise ValueError("Training cannot award the exam qualification")
    elif pool_id == LEGACY_EXAM_POOL or (pool_id, skill_id, skill_name) != (EXAM_POOL, EXAM_SKILL, SKILL_NAME):
        raise ValueError("Only the current five-photo exam can be synchronized. "
                         "Use score.py --exam-version 10 for old exports.")
    pool = api.request("GET", "pools/" + pool_id)
    skill = api.request("GET", "skills/" + skill_id)
    if pool["id"] != pool_id or pool["project_id"] != PROJECT:
        raise ValueError("Unexpected pool or project")
    if not training and pool.get("type") == "TRAINING":
        raise ValueError("Training pool selected for the exam")
    if not training and ledger is not None and pool.get("auto_accept_solutions") is not False:
        raise ValueError("Configure deferred acceptance in the exam pool before running automatic review")
    if skill["id"] != skill_id or skill["name"] != skill_name or skill.get("deprecated"):
        raise ValueError("Unexpected qualification skill")
    assignments = completed_assignments(api, pool_id)
    statuses = {item["id"]: item["status"] for item in assignments}
    report = score.score_assignments(assignments, gold, training=training)
    report.update(pool_id=pool_id, skill_id=skill_id, stage="training" if training else "exam")
    if ledger is not None:
        init_ledger(ledger)
    for attempt in report["attempts"]:
        if not training:
            status = statuses[attempt["assignment_id"]]
            attempt["payment_status"] = status
            if status == "SUBMITTED" and attempt["decision"] in {"pass", "fail"}:
                if ledger is None:
                    attempt["planned_review"] = "ACCEPTED" if attempt["decision"] == "pass" else "REJECTED"
                else:
                    status = attempt["payment_status"] = review_exam(api, ledger, attempt)
            if status != "ACCEPTED":
                continue
        if attempt["decision"] == "pass":
            attempt["grant"] = ("would_grant" if ledger is None else
                                grant(api, ledger, pool_id, skill_id, attempt))
    return report


def worker(args, api, stages, db):
    while True:
        failed = False
        for pool_id, skill_id, name, gold, training in stages:
            try:
                result = run_once(api, pool_id, skill_id, name, gold, db, training=training)
                print(json.dumps(result, ensure_ascii=False), flush=True)
            except HTTPError as exc:
                print(f"Yandex Tasks HTTP {exc.code} (pool {pool_id})", file=sys.stderr, flush=True)
                if exc.code != 429 and exc.code < 500:
                    return 1
                failed = True
            except (URLError, TimeoutError, ConnectionError, RuntimeError) as exc:
                print(f"Pool {pool_id}: {type(exc).__name__}", file=sys.stderr, flush=True)
                failed = True
        if not args.watch:
            return int(failed)
        time.sleep(args.watch)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", default=EXAM_POOL, help=f"Exam pool ID (default {EXAM_POOL})")
    parser.add_argument("--skill", default=EXAM_SKILL, help=f"Exam skill ID (default {EXAM_SKILL})")
    parser.add_argument("--skill-name", default=SKILL_NAME, help="Exact name of the skill")
    parser.add_argument("--stage", choices=("both", "training", "exam"), default="both",
                        help="Qualification stages to process (default: both)")
    parser.add_argument("--apply", action="store_true", help="Accept or reject graded exams and grant skills")
    parser.add_argument("--watch", type=int, metavar="SECONDS", help="Repeat until interrupted")
    parser.add_argument("--state", type=Path, default=score.GOLD.with_name("grants.sqlite3"))
    args = parser.parse_args()
    if args.watch is not None and args.watch < 1:
        parser.error("--watch requires a positive interval")
    token = os.environ.get("YANDEX_TASKS_TOKEN")
    if not token and sys.stdin.isatty():
        token = getpass.getpass("OAuth-токен Яндекс Заданий: ").strip()
    if not token:
        parser.error("Set YANDEX_TASKS_TOKEN in the process environment")
    api = Api(token)
    try:
        stages = []
        if args.stage in ("both", "training"):
            stages.append((TRAINING_POOL, TRAINING_SKILL, TRAINING_NAME,
                           json.loads(score.TRAINING_GOLD.read_text(encoding="utf-8")), True))
        if args.stage in ("both", "exam"):
            stages.append((args.pool, args.skill, args.skill_name,
                           json.loads(score.GOLD.read_text(encoding="utf-8")), False))
        if args.apply:
            with closing(sqlite3.connect(args.state, timeout=60)) as db:
                return worker(args, api, stages, db)
        return worker(args, api, stages, None)
    except KeyboardInterrupt:
        return 0
    except (ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        print(f"Cannot synchronize: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
