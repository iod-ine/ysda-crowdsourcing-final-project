"""Merge the team's fixes (JSON exported from golden_annotator.html) into the final golden set + corrections log.

Usage: python3 -I merge_team_fixes.py results/team_export_*.json
  - every photo from results/team_queue.json must be in the export(s) with «Сомневаюсь» unchecked (doubt=false),
    otherwise it is listed as unresolved and stays out of the golden set;
  - if one photo is in several exports, the one with the latest updated_at wins.
Outputs: results/golden_final.json (butterfly-bbox-golden/v1, all resolved photos),
         results/corrections_log.csv (what changed on each team photo and why), printed summary.
"""
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RES = HERE / "results"
KEYS = ("left", "top", "width", "height")


def iou(a, b):
    ax1, ay1, bx1, by1 = a["left"] + a["width"], a["top"] + a["height"], b["left"] + b["width"], b["top"] + b["height"]
    iw, ih = max(0.0, min(ax1, bx1) - max(a["left"], b["left"])), max(0.0, min(ay1, by1) - max(a["top"], b["top"]))
    inter = iw * ih
    union = a["width"] * a["height"] + b["width"] * b["height"] - inter
    return inter / union if union > 0 else 0.0


def describe(before, after):
    """Global greedy IoU matching (best pairs first): kept / moved / removed / added boxes."""
    pairs = sorted(((iou(b, a), i, j) for i, b in enumerate(before) for j, a in enumerate(after)), reverse=True)
    used_b, used_a, kept, moved = set(), set(), 0, 0
    for v, i, j in pairs:
        if v < 0.5:
            break
        if i in used_b or j in used_a:
            continue
        used_b.add(i); used_a.add(j)
        if v >= 0.98:
            kept += 1
        else:
            moved += 1
    removed, added = len(before) - len(used_b), len(after) - len(used_a)
    parts = [f"{n} {w}" for n, w in ((moved, "подвинуто"), (removed, "удалено"), (added, "добавлено")) if n]
    return ", ".join(parts) if parts else "без изменений"


if len(sys.argv) < 2:
    raise SystemExit(__doc__)
auto = json.loads((RES / "golden_auto.json").read_text())
queue = {i["photo_id"]: i for i in json.loads((RES / "team_queue.json").read_text())["items"]}
decisions = {int(d["photo_id"]): d for d in json.loads((RES / "decisions.json").read_text())}

team, annotators, skipped = {}, set(), 0
for path in sys.argv[1:]:
    exp = json.loads(Path(path).read_text())
    assert exp.get("format") == "butterfly-bbox-golden/v1", f"{path}: not an annotator export"
    for it in exp["items"]:
        pid = int(it["photo_id"])
        if pid not in queue:      # other photos left in the annotator from earlier work
            skipped += 1
            continue
        it["_annotator"] = exp.get("annotator") or "?"
        if pid not in team or (it.get("updated_at") or "") > (team[pid].get("updated_at") or ""):
            team[pid] = it

resolved, unresolved, log = [], [], []
for pid, q in queue.items():
    it = team.get(pid)
    if it is None or it.get("doubt") or it.get("status") not in ("boxes", "empty"):
        unresolved.append((pid, "нет в выгрузке" if it is None else ("сомнение не снято" if it.get("doubt") else f"статус {it.get('status')}")))
        continue
    after = [{k: float(b[k]) for k in KEYS} for b in it.get("result") or []] if it["status"] == "boxes" else []
    before = [{k: float(b[k]) for k in KEYS} for b in decisions[pid]["labeler_boxes_list"]]
    change = describe(before, after)
    resolved.append({"image": it["image"], "photo_id": pid, "status": "boxes" if after else "empty", "doubt": False, "comment": it.get("comment", ""),
                     "result": [{"shape": "rectangle", **{k: round(b[k], 6) for k in KEYS}} for b in after], "source": "team",
                     "review_votes": decisions[pid]["votes"], "review_reason": q["reason"], "annotator": it["_annotator"],
                     "image_width": it.get("image_width"), "image_height": it.get("image_height"), "updated_at": it.get("updated_at")})
    log.append({"photo_id": pid, "image": it["image"], "why_in_queue": q["reason"], "review_votes": json.dumps(decisions[pid]["votes"], ensure_ascii=False),
                "boxes_before": len(before), "boxes_after": len(after), "change": change, "annotator": it["_annotator"],
                "team_comment": it.get("comment", ""), "updated_at": it.get("updated_at") or ""})
    annotators.add(it["_annotator"])

items = sorted(auto["items"] + resolved, key=lambda x: x["photo_id"])
cnt = lambda s: sum(1 for i in items if i["status"] == s)
(RES / "golden_final.json").write_text(json.dumps({
    "format": "butterfly-bbox-golden/v1", "annotator": "review-aggregation + team: " + ", ".join(sorted(annotators)),
    "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "counts": {"total": len(items), "boxes": cnt("boxes"), "empty": cnt("empty"), "unresolved": len(unresolved),
               "butterflies": sum(len(i["result"]) for i in items)},
    "items": items}, ensure_ascii=False, indent=1))
with (RES / "corrections_log.csv").open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(log[0]) if log else ["photo_id"])
    w.writeheader()
    w.writerows(sorted(log, key=lambda r: r["photo_id"]))
changed = sum(1 for r in log if r["change"] != "без изменений")
if skipped:
    print(f"пропущено {skipped} фото из выгрузки: их нет в очереди команды")
print(f"golden_final.json: {len(items)} фото (с бабочками {cnt('boxes')}, без бабочек {cnt('empty')}, рамок {sum(len(i['result']) for i in items)})")
print(f"команда разобрала {len(resolved)} из {len(queue)}: исправлено {changed}, оставлено как было {len(resolved) - changed}")
if unresolved:
    print("НЕ РАЗОБРАНЫ (в эталон не попали):", unresolved)
