"""Build the hand-off zip with three JSON files:
  review_answers.json — every reviewer answer on the 400 photos (main review pool), with the decision per photo;
  golden_set.json     — the final golden set (copy of results/golden_final.json);
  golden_stats.json   — statistics of the golden set and of how it was obtained.
Inputs: results/raw/*.json (from aggregate_review.py), results/decisions.json, results/golden_final.json,
results/corrections_log.csv. Usage: python3 -I build_bundle.py
"""
import csv
import json
import statistics as st
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RES = HERE / "results"
ZIP = HERE / "butterfly_golden_2026-11-19.zip"
LABEL = {"ok": "Всё верно", "missed": "Пропущена бабочка", "extra": "Лишняя рамка", "box": "Неточная рамка",
         "load_error": "Фото не загрузилось"}
now = datetime.now(timezone.utc).isoformat(timespec="seconds")
# reviewer IDs are replaced by R01..R17 (local file reviewer_ids.json, not published)
REVIEWER = json.loads((HERE / "reviewer_ids.json").read_text())
CODE = lambda uid: REVIEWER.get(uid, "R??")

tasks = json.loads((RES / "raw" / "main_pool_tasks.json").read_text())
asg = json.loads((RES / "raw" / "main_pool_assignments.json").read_text())
decisions = {d["task_id"]: d for d in json.loads((RES / "decisions.json").read_text())}
golden = json.loads((RES / "golden_final.json").read_text())
corrections = list(csv.DictReader((RES / "corrections_log.csv").open()))
boxes_of = lambda v: [{k: b[k] for k in ("left", "top", "width", "height")} for b in (json.loads(v) if isinstance(v, str) else (v or []))]

# 1. reviewer answers
answers = {tid: [] for tid in decisions}
reviewers = {}
for a in asg:
    if a["status"] not in ("ACCEPTED", "REJECTED"):
        continue
    r = reviewers.setdefault(CODE(a["user_id"]), {"pages_accepted": 0, "pages_rejected": 0})
    r["pages_accepted" if a["status"] == "ACCEPTED" else "pages_rejected"] += 1
    for t, s in zip(a["tasks"], a["solutions"]):
        if t["id"] not in answers:          # control photos are not part of the 400
            continue
        ov = s["output_values"]
        ans = {"reviewer": CODE(a["user_id"]), "verdict": ov.get("verdict"), "comment": (ov.get("comment") or "").strip(),
               "page_status": a["status"], "submitted": a.get("submitted")}
        if ov.get("verdict") == "box":
            ans["boxes"] = boxes_of(ov.get("result"))
        answers[t["id"]].append(ans)
photos = []
for tid, d in sorted(decisions.items(), key=lambda kv: int(kv[1]["photo_id"])):
    photos.append({"photo_id": int(d["photo_id"]), "image": d["image"], "labeler_boxes": [
        {k: b[k] for k in ("left", "top", "width", "height")} for b in d["labeler_boxes_list"]],
        "answers": sorted(answers[tid], key=lambda x: x["submitted"] or ""),
        "votes_counted": d["votes"], "decision": "accepted_by_review" if d["decision"] == "golden" else "manual_review",
        "reason": d["reason"]})
review_answers = {
    "format": "butterfly-review-answers/v1", "exported_at": now,
    "source": "Я.Задания, проект 11943 «Проверьте, правильно ли выделены бабочки», пул 6871826",
    "verdicts": LABEL,
    "rules": "Голоса считаются только со страниц ACCEPTED; «Фото не загрузилось» отброшено, если фото открывается. "
             "Решение по большинству; спорные фото разобраны вручную (см. golden_stats.json → manual_review).",
    "boxes_format": "left, top, width, height — доли от ширины и высоты фото, повёрнутого по EXIF",
    "counts": {"photos": len(photos), "answers": sum(len(p["answers"]) for p in photos),
               "answers_counted": sum(sum(p["votes_counted"].values()) for p in photos), "reviewers": len(reviewers)},
    "reviewers": reviewers, "photos": photos}

# 2. golden set: as is
# 3. statistics
items = golden["items"]
bxs = [b for i in items for b in i["result"]]
area = [b["width"] * b["height"] for b in bxs]
q = st.quantiles(area, n=10)
per_photo = Counter(min(len(i["result"]), 3) for i in items)
votes_n = Counter(sum(d["votes"].values()) for d in decisions.values())
agree = Counter()
for d in decisions.values():
    v, n = d["votes"], sum(d["votes"].values())
    agree["unanimous" if n and max(v.values()) == n else "majority" if n and max(v.values()) * 2 > n else "no_majority"] += 1
change = Counter()
for r in corrections:
    for part in r["change"].split(", "):
        if part != "без изменений":
            k, what = part.split(" ", 1)
            change[{"подвинуто": "boxes_moved", "удалено": "boxes_removed", "добавлено": "boxes_added"}[what]] += int(k)
stats = {
    "format": "butterfly-golden-stats/v1", "exported_at": now, "golden_set": "golden_set.json",
    "photos": {"total": len(items), "with_butterflies": sum(1 for i in items if i["result"]),
               "without_butterflies": sum(1 for i in items if not i["result"])},
    "butterflies": {"total": len(bxs),
                    "per_photo": {"0": per_photo[0], "1": per_photo[1], "2": per_photo[2], "3+": per_photo[3]},
                    "size_share_of_frame": {"large_over_15pct": sum(a >= 0.15 for a in area),
                                            "medium_2_to_15pct": sum(0.02 <= a < 0.15 for a in area),
                                            "small_under_2pct": sum(a < 0.02 for a in area)},
                    "box_area_share_quantiles": {"p10": round(q[0], 4), "median": round(st.median(area), 4), "p90": round(q[8], 4)},
                    "cut_by_frame_edge": sum(1 for b in bxs if b["left"] < 0.005 or b["top"] < 0.005
                                             or b["left"] + b["width"] > 0.995 or b["top"] + b["height"] > 0.995)},
    "review": {"reviewers_with_accepted_pages": sum(1 for r in reviewers.values() if r["pages_accepted"]),
               "pages_paid": sum(r["pages_accepted"] for r in reviewers.values()),
               "pages_rejected": sum(r["pages_rejected"] for r in reviewers.values()),
               "counted_votes_per_photo": {str(k): votes_n[k] for k in sorted(votes_n)},
               "agreement": dict(agree),
               "accepted_by_review": sum(1 for i in items if i.get("source") == "review")},
    "manual_review": {"photos": len(corrections),
                      "changed": sum(1 for r in corrections if r["change"] != "без изменений"),
                      "confirmed_as_is": sum(1 for r in corrections if r["change"] == "без изменений"),
                      **{k: change[k] for k in ("boxes_added", "boxes_removed", "boxes_moved")},
                      "log": [{"photo_id": int(r["photo_id"]), "why": r["why_in_queue"], "change": r["change"],
                               "boxes_before": int(r["boxes_before"]), "boxes_after": int(r["boxes_after"])} for r in corrections]},
}

files = {"review_answers.json": review_answers, "golden_set.json": golden, "golden_stats.json": stats}
for name, obj in files.items():
    (RES / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1))
with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
    for name in files:
        z.write(RES / name, name)
print(json.dumps({k: v for k, v in stats.items() if k != "manual_review"}, ensure_ascii=False, indent=1))
print("manual_review:", {k: v for k, v in stats["manual_review"].items() if k != "log"})
print("review_answers counts:", review_answers["counts"])
print(ZIP.name, f"{ZIP.stat().st_size // 1024} KB:", [i.filename for i in zipfile.ZipFile(ZIP).infolist()])
