"""Aggregate the review of the 400 labeled photos (main review pool) into decisions, a golden set and a team-fix queue.

Read-only on the platform (GET only). Inputs: tasks and assignments of the main review pool.
Rules (agreed with the team, see budget page):
  - only answers from ACCEPTED pages count (rejected pages = reviewers below the control threshold);
  - «Фото не загрузилось» votes are dropped when the image actually loads;
  - majority «Всё верно» -> labeler's boxes go to the golden set, unless a minority found a missed butterfly,
    a minority named an extra box in the comment (except a reviewer who marks moths as non-butterflies),
    or our own quality check found a problem there -> the team looks;
  - majority «Неточная рамка» -> median of the boxes the reviewers actually moved; nobody moved -> team;
  - majority «Пропущена бабочка» / «Лишняя рамка», or no majority -> team fixes in golden_annotator.html.
Outputs in results/: votes.csv, reviewers.csv, decisions.json, golden_auto.json (butterfly-bbox-golden/v1),
team_queue.json (open in golden_annotator.html via «Загрузить задания»), summary.txt.
Usage: python3 -I aggregate_review.py
"""
import csv
import json
import os
import statistics as st
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
OUT = HERE / "results"
RAW = OUT / "raw"
RAW.mkdir(parents=True, exist_ok=True)
TOKEN = Path(os.environ["YA_TASKS_TOKEN_FILE"]).expanduser().read_text().strip()   # file with the OAuth token
PID = "6871826"                                     # main review pool
# reviewer IDs are replaced by R01..R17 (local file reviewer_ids.json, not published)
REVIEWER = json.loads((HERE / "reviewer_ids.json").read_text())
CODE = lambda uid: REVIEWER.get(uid, "R??")
BROKEN_CONTROL_PHOTOS = {"388813999", "761346880"}   # disabled controls, not used to judge reviewers
# these reviewers mark moths/skippers as «не бабочка» (moths count as butterflies); checked by eye on their flags
# (R11: 12 of 12 moths, R10: 4 of 4 moths/skipper), so their minority «Лишняя рамка» is not a signal
MOTH_BLIND = {"R10", "R11"}
# boxes on non-butterflies found in our own quality check of the labeling, confirmed by eye
KNOWN_ISSUES = {"762664826": "рамка на пчеле (наша проверка качества разметки)",
                "711175722": "рамка на небе (наша проверка качества разметки)",
                "703071576": "вероятно, рамка на траве (наша проверка качества разметки)"}
# first look at the team queue (rendered photos, 2026-11-19): a hint only, the team makes the decision.
# priority 1 = clearly needs a fix, 2 = zoom into the original and decide, 3 = most likely keep as is
MOTH_OK = "ночная бабочка (моль), рамка выглядит верной — скорее всего оставить как есть"
HINTS = {
    "762098330": (1, "пропущена толстоголовка на одуванчике — нарисовать рамку"),
    "762641966": (1, "пропущена жёлто-оранжевая бабочка в центре — нарисовать рамку"),
    "762445055": (1, "одна бабочка, две рамки — удалить меньшую рамку 1"),
    "760759513": (1, "похоже, одна бабочка с крыльями в разные стороны — заменить две рамки одной на всю бабочку"),
    "762664826": (1, "пчела, не бабочка — удалить рамку (фото без бабочек)"),
    "711175722": (2, "точка в небе — увеличить оригинал; если не видно бабочку, удалить рамку"),
    "703071576": (2, "маленький тёмный объект в траве — увеличить оригинал; если не бабочка, удалить рамку"),
    "758718174": (2, "размытое пятно на стене — увеличить оригинал; если не видно бабочку, удалить рамку"),
    "477078360": (2, "рамка 1 на крыле второй бабочки за первой? проверить, нет ли ещё бабочки"),
    "731273614": (2, "две жёлтые рамки — убедиться, что обе на бабочках, а не на жёлтых листьях"),
    "762421600": (2, "жёлтые листья, рамок нет; один ревьюер нашёл бабочку — внимательно осмотреть"),
    "757765903": (2, "двое отметили неточную рамку — проверить левую границу"),
    "761939431": (3, "толстоголовка, рамка верная — скорее всего оставить"),
    "762517963": (3, "маленькая бабочка в траве, рамка верная — скорее всего оставить"),
    "762598738": (3, "крупная моль, рамка верная, пропуска не видно — скорее всего оставить"),
    **{p: (3, MOTH_OK) for p in ("173460760", "275215939", "602739504", "746690362", "761781835",
                                 "762665997", "762668953", "762685653")},
}
LABEL = {"ok": "Всё верно", "missed": "Пропущена бабочка", "extra": "Лишняя рамка", "box": "Неточная рамка",
         "load_error": "Фото не загрузилось"}


def get(ep, **q):
    req = Request("https://tasks.yandex.ru/api/v1" + ep + ("?" + urlencode(q) if q else ""), headers={"Authorization": "OAuth " + TOKEN})
    with urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def list_all(ep, **q):
    items = []
    while True:
        res = get(ep, limit=100, sort="id", **q)
        items += res["items"]
        if not res.get("has_more") or not res["items"]:
            return items
        q["id_gt"] = res["items"][-1]["id"]


def boxes_of(v):
    v = json.loads(v) if isinstance(v, str) else (v or [])
    return [{k: float(b[k]) for k in ("left", "top", "width", "height")} for b in v if b and b.get("shape", "rectangle") == "rectangle"]


def iou(a, b):
    ax1, ay1, bx1, by1 = a["left"] + a["width"], a["top"] + a["height"], b["left"] + b["width"], b["top"] + b["height"]
    iw, ih = max(0.0, min(ax1, bx1) - max(a["left"], b["left"])), max(0.0, min(ay1, by1) - max(a["top"], b["top"]))
    inter = iw * ih
    union = a["width"] * a["height"] + b["width"] * b["height"] - inter
    return inter / union if union > 0 else 0.0


def same_boxes(a, b, tol=1e-4):
    return len(a) == len(b) and all(abs(x[k] - y[k]) <= tol for x, y in zip(a, b) for k in x)


def image_loads(url):
    try:
        with urlopen(Request(url, method="HEAD"), timeout=30) as r:
            return r.status == 200 and r.headers.get("Content-Type", "").startswith("image/")
    except (HTTPError, URLError, TimeoutError):
        return False


def median_boxes(base, edits):
    """Each edited set is matched to the labeler's boxes by IoU; the result is the per-coordinate median of the edges."""
    out = []
    for i, b in enumerate(base):
        cands = []
        for e in edits:
            m = max(e, key=lambda x: iou(x, b), default=None)
            if m is not None and iou(m, b) > 0.3:
                cands.append(m)
        if not cands:
            out.append(dict(b))
            continue
        x0 = st.median(c["left"] for c in cands); y0 = st.median(c["top"] for c in cands)
        x1 = st.median(c["left"] + c["width"] for c in cands); y1 = st.median(c["top"] + c["height"] for c in cands)
        out.append({"left": x0, "top": y0, "width": x1 - x0, "height": y1 - y0})
    return out


r6 = lambda x: round(x, 6)
to_golden_boxes = lambda bs: [{"shape": "rectangle", **{k: r6(b[k]) for k in ("left", "top", "width", "height")}} for b in bs]

if os.environ.get("YA_TASKS_REQUESTER_ID"):          # optional guard against a wrong account
    assert get("/requester")["id"] == os.environ["YA_TASKS_REQUESTER_ID"]
tasks = list_all("/tasks", pool_id=PID)
asg = list_all("/assignments", pool_id=PID)
(RAW / "main_pool_tasks.json").write_text(json.dumps(tasks, ensure_ascii=False))
(RAW / "main_pool_assignments.json").write_text(json.dumps(asg, ensure_ascii=False))
real = {t["id"]: t for t in tasks if not t.get("known_solutions")}

# reviewer quality on controls (broken controls excluded)
rev = defaultdict(lambda: {"accepted": 0, "rejected": 0, "ctl_ok": 0, "ctl_n": 0, "verdicts": Counter()})
votes = defaultdict(list)
for a in asg:
    if a["status"] not in ("ACCEPTED", "REJECTED"):
        continue
    r = rev[CODE(a["user_id"])]
    r["accepted" if a["status"] == "ACCEPTED" else "rejected"] += 1
    for t, s in zip(a["tasks"], a["solutions"]):
        ov = s["output_values"]
        if t.get("known_solutions"):
            if t["input_values"]["photo_id"] not in BROKEN_CONTROL_PHOTOS:
                r["ctl_n"] += 1
                r["ctl_ok"] += ov.get("verdict") == t["known_solutions"][0]["output_values"]["verdict"]
            continue
        if a["status"] != "ACCEPTED":
            continue
        r["verdicts"][ov.get("verdict")] += 1
        votes[t["id"]].append({"user": CODE(a["user_id"]), "verdict": ov.get("verdict"), "comment": (ov.get("comment") or "").strip(),
                               "result": boxes_of(ov.get("result")) if ov.get("verdict") == "box" else None})

# drop «not loaded» votes for images that do load
load_checked = {}
for tid, vs in votes.items():
    if any(v["verdict"] == "load_error" for v in vs):
        url = real[tid]["input_values"]["image"]
        load_checked[url] = image_loads(url)
        if load_checked[url]:
            votes[tid] = [v for v in vs if v["verdict"] != "load_error"]

decisions, team, golden_items = [], [], []
for tid, t in real.items():
    iv = t["input_values"]
    base = boxes_of(iv["boxes"])
    vs = votes.get(tid, [])
    c = Counter(v["verdict"] for v in vs)
    n = len(vs)
    top, top_n = c.most_common(1)[0] if c else (None, 0)
    majority = top if n and top_n * 2 > n else None
    edits = [v["result"] for v in vs if v["verdict"] == "box" and v["result"] and not same_boxes(v["result"], base)]
    final, todo = None, None
    if n == 0:
        todo = "нет проверок"
    elif majority == "ok":
        named_extra = [v for v in vs if v["verdict"] == "extra" and v["comment"] and v["user"] not in MOTH_BLIND]
        if c["missed"]:
            todo = "большинство «Всё верно», но кто-то нашёл пропущенную бабочку — проверить"
        elif iv["photo_id"] in KNOWN_ISSUES:
            todo = KNOWN_ISSUES[iv["photo_id"]] + " — проверить"
        elif named_extra:
            todo = "большинство «Всё верно», но кто-то указал лишнюю рамку — проверить"
        else:
            final, how = base, "принято: большинство «Всё верно»"
    elif majority == "box":
        if edits:
            final, how = median_boxes(base, edits), f"рамки поправлены ревьюерами (медиана {len(edits)} исправл.)"
        else:
            todo = "неточная рамка, но ревьюеры её не подвинули — поправить"
    elif majority == "missed":
        todo = "пропущена бабочка — дорисовать рамку"
    elif majority == "extra":
        todo = "лишняя рамка — удалить"
    elif majority == "load_error":
        todo = "фото не загрузилось у большинства — проверить фото"
    else:
        todo = "нет большинства — решить самим"
    comments = [f"{LABEL.get(v['verdict'], v['verdict'])}: {v['comment']}" for v in vs if v["comment"]]
    vote_str = ", ".join(f"{LABEL.get(k, k)} ×{m}" for k, m in c.most_common())
    d = {"task_id": tid, "photo_id": iv["photo_id"], "image": iv["image"], "labeler_boxes": len(base), "votes": dict(c),
         "n_votes": n, "majority": majority, "decision": "golden" if final is not None else "team", "reason": how if final is not None else todo,
         "comments": comments, "final_boxes": to_golden_boxes(final) if final is not None else None, "labeler_boxes_list": to_golden_boxes(base),
         "box_edits": len(edits)}
    decisions.append(d)
    if final is not None:
        golden_items.append({"image": iv["image"], "photo_id": int(iv["photo_id"]), "status": "boxes" if final else "empty",
                             "doubt": False, "comment": "", "result": to_golden_boxes(final), "source": "review",
                             "review_votes": dict(c), "review_reason": how})
    else:
        start = median_boxes(base, edits) if edits else base
        prio, hint = HINTS.get(iv["photo_id"], (2, ""))
        note = (f"Подсказка: {hint}. " if hint else "") + f"Почему здесь: {todo}. Голоса: {vote_str}." + \
            (" Комментарии ревьюеров: " + " | ".join(comments) if comments else "")
        d["priority"], d["hint"] = prio, hint
        team.append({"image": iv["image"], "photo_id": int(iv["photo_id"]), "status": "boxes" if start else "empty",
                     "doubt": True, "comment": note, "result": to_golden_boxes(start), "priority": prio, "reason": todo})

now = datetime.now(timezone.utc).isoformat(timespec="seconds")
golden_items.sort(key=lambda x: x["photo_id"])
team.sort(key=lambda x: (x["priority"], x["photo_id"]))
cnt = lambda items, s: sum(1 for i in items if i["status"] == s)
(OUT / "golden_auto.json").write_text(json.dumps({
    "format": "butterfly-bbox-golden/v1", "annotator": "review-aggregation", "exported_at": now,
    "counts": {"total": len(golden_items), "boxes": cnt(golden_items, "boxes"), "empty": cnt(golden_items, "empty")},
    "items": golden_items}, ensure_ascii=False, indent=1))
(OUT / "team_queue.json").write_text(json.dumps({
    "format": "butterfly-bbox-golden/v1", "annotator": "", "exported_at": now,
    "counts": {"total": len(team)}, "items": team}, ensure_ascii=False, indent=1))
(OUT / "decisions.json").write_text(json.dumps(sorted(decisions, key=lambda d: int(d["photo_id"])), ensure_ascii=False, indent=1))
with (OUT / "votes.csv").open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["photo_id", "image", "labeler_boxes", "n_votes", "ok", "missed", "extra", "box", "load_error", "majority", "decision", "reason", "comments"])
    for d in sorted(decisions, key=lambda d: int(d["photo_id"])):
        v = d["votes"]
        w.writerow([d["photo_id"], d["image"], d["labeler_boxes"], d["n_votes"]] + [v.get(k, 0) for k in LABEL] +
                   [d["majority"] or "", d["decision"], d["reason"], " | ".join(d["comments"])])
with (OUT / "reviewers.csv").open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["reviewer", "pages_accepted", "pages_rejected", "controls_right", "controls_total", "controls_rate"] + list(LABEL))
    for u, r in sorted(rev.items(), key=lambda kv: -kv[1]["accepted"]):
        w.writerow([u, r["accepted"], r["rejected"], r["ctl_ok"], r["ctl_n"], round(r["ctl_ok"] / r["ctl_n"], 3) if r["ctl_n"] else ""] +
                   [r["verdicts"].get(k, 0) for k in LABEL])

reasons = Counter(d["reason"] for d in decisions)
lines = [f"Фото: {len(decisions)}; проверок на фото: {dict(sorted(Counter(d['n_votes'] for d in decisions).items()))}",
         f"В эталон автоматически: {len(golden_items)} (с бабочками {cnt(golden_items, 'boxes')}, без бабочек {cnt(golden_items, 'empty')})",
         f"Команде на исправление: {len(team)}",
         "Причины:"] + [f"  {m:4d}  {k}" for k, m in reasons.most_common()] + [
         f"Голоса «не загрузилось» отброшены, фото открываются: {sum(load_checked.values())} из {len(load_checked)}",
         f"Ревьюеров с принятыми страницами: {sum(1 for r in rev.values() if r['accepted'])}"]
(OUT / "summary.txt").write_text("\n".join(lines) + "\n")
print("\n".join(lines))
