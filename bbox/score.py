"""Read-only geometry assessment for BBOX practice and exam pages."""

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


GOLD = Path(__file__).resolve().parent / "private" / "gold5.json"
LEGACY_GOLD = GOLD.with_name("gold.json")
TRAINING_GOLD = GOLD.with_name("training-gold.json")
EXAM_IOU = 0.75
EXAM_THRESHOLDS = {EXAM_IOU: 1.00}
LEGACY_THRESHOLDS = {0.5: 0.90, 0.75: 0.80}
TRAINING_THRESHOLDS = {0.75: 0.80}
SCORABLE_STATES = {"SUBMITTED", "ACCEPTED", "APPROVED"}


def parse_boxes(raw):
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        try:
            boxes = json.loads(raw)
        except json.JSONDecodeError:
            # TSV array_json uses an unescaped comma between items.
            boxes = [json.loads(item.replace("\\,", ","))
                     for item in re.split(r"(?<!\\),", raw)]
        if isinstance(boxes, dict):
            boxes = [boxes]
    else:
        boxes = raw
    if not isinstance(boxes, list):
        raise ValueError("result must be an array")
    for box in boxes:
        if not isinstance(box, dict) or box.get("shape") != "rectangle":
            raise ValueError("expected a rectangle")
        coords = [box.get(k) for k in ("left", "top", "width", "height")]
        if not all(type(n) in (int, float) and 0 <= n <= 1 + 1e-8 for n in coords):
            raise ValueError("invalid rectangle coordinates")
        x, y, w, h = coords
        if min(x, y) < 0 or min(w, h) <= 0 or max(x + w, y + h) > 1 + 1e-8:
            raise ValueError("empty rectangle or coordinates outside the image")
    return boxes


def iou(a, b):
    dx = min(a["left"] + a["width"], b["left"] + b["width"]) - max(a["left"], b["left"])
    dy = min(a["top"] + a["height"], b["top"] + b["height"]) - max(a["top"], b["top"])
    common = max(dx, 0) * max(dy, 0)
    return common / (a["width"] * a["height"] + b["width"] * b["height"] - common)


def match_count(predicted, reference, threshold):
    # Normalized float coordinates can put an exact boundary a few ulps below the cutoff.
    edges = [[i for i, gold in enumerate(reference) if iou(box, gold) >= threshold - 1e-12]
             for box in predicted]
    owner = {}

    def find_match(index, visited):
        for slot in edges[index]:
            if slot in visited:
                continue
            visited.add(slot)
            if slot not in owner or find_match(owner[slot], visited):
                owner[slot] = index
                return True
        return False

    return sum(find_match(index, set()) for index in range(len(predicted)))


def read_attempts(source):
    if source.suffix.lower() == ".tsv":
        attempts = defaultdict(list)
        with source.open(encoding="utf-8-sig", newline="") as handle:
            table = csv.DictReader(handle, delimiter="\t")
            required = {"ASSIGNMENT:assignment_id", "ASSIGNMENT:worker_id", "ASSIGNMENT:status",
                        "INPUT:image", "OUTPUT:status", "OUTPUT:result"}
            missing = required.difference(table.fieldnames or ())
            if missing:
                raise ValueError("Missing TSV columns: " + ", ".join(sorted(missing)))
            for row in table:
                attempts[row["ASSIGNMENT:assignment_id"]].append(row)
        for assignment_id, rows in attempts.items():
            workers = {row["ASSIGNMENT:worker_id"] for row in rows}
            states = {row["ASSIGNMENT:status"] for row in rows}
            if len(workers) != 1 or len(states) != 1:
                raise ValueError(f"Conflicting assignment metadata for {assignment_id}")
            outputs = [(r["INPUT:image"], r["OUTPUT:status"], r["OUTPUT:result"]) for r in rows]
            yield assignment_id, workers.pop(), states.pop(), outputs
        return
    with source.open(encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    if isinstance(payload, list):
        assignments = payload
    elif "items" in payload:
        if payload.get("has_more"):
            raise ValueError("Incomplete API export. Collect all pages before scoring.")
        assignments = payload["items"]
    else:
        assignments = [payload]
    yield from api_attempts(assignments)


def api_attempts(assignments):
    for item in assignments:
        tasks, solutions = item.get("tasks"), item.get("solutions")
        outputs = []
        if isinstance(tasks, list) and isinstance(solutions, list) and len(tasks) == len(solutions):
            for task, solution in zip(tasks, solutions):
                inputs = task.get("input_values") if isinstance(task, dict) else None
                data = solution.get("output_values") if isinstance(solution, dict) else None
                if not isinstance(inputs, dict) or not isinstance(inputs.get("image"), str):
                    break
                if not isinstance(data, dict):
                    break
                outputs.append((inputs["image"], data.get("status"), data.get("result")))
            if len(outputs) != len(tasks):
                outputs.clear()
        yield item["id"], item["user_id"], item["status"], outputs


def assess(gold, assignment_id, worker_id, state, answers, thresholds):
    report = {"assignment_id": assignment_id, "worker_id": worker_id,
              "decision": "manual_review", "issues": [], "images": []}
    issues = report["issues"]
    if state not in SCORABLE_STATES:
        issues.append(f"Assignment status: {state}")
    urls = Counter(row[0] for row in answers)
    if urls != Counter(gold.keys()):
        issues.append("Missing, duplicated or unexpected images on this page")
        return report
    scores = {threshold: Counter() for threshold in thresholds}
    empty_fp = 0
    for url, status, raw in answers:
        record = gold[url]
        detail = {"photo_id": record["photo_id"], "status": status}
        report["images"].append(detail)
        try:
            boxes = parse_boxes(raw)
            if status not in {"found", "none"}:
                raise ValueError(f"unresolved response: {status}")
            if (status == "found") != bool(boxes):
                raise ValueError("status and boxes do not match")
        except (ValueError, TypeError) as exc:
            issues.append(f"{record['photo_id']}: {exc}")
            continue
        if not record["boxes"]:
            empty_fp += len(boxes)
        for threshold in scores:
            tp = match_count(boxes, record["boxes"], threshold)
            counts = {"tp": tp, "fp": len(boxes) - tp, "fn": len(record["boxes"]) - tp}
            detail[f"IoU{threshold}"] = counts
            scores[threshold].update(counts)
    if issues:
        return report
    report["false_boxes_on_empty"] = empty_fp
    passed = empty_fp == 0
    for threshold, counts in scores.items():
        tp, fp, fn = (counts[key] for key in ("tp", "fp", "fn"))
        f1 = 2 * tp / (2 * tp + fp + fn)  # The fixed exam has positive objects.
        report[f"IoU{threshold}"] = {**counts, "f1": f1}
        passed = passed and f1 >= thresholds[threshold]
    report["decision"] = "pass" if passed else "fail"
    return report


def score_attempts(attempts, gold, *, training=False, exam_version=5):
    if training:
        thresholds = TRAINING_THRESHOLDS
    elif exam_version == 10 and len(gold) == 10:
        thresholds = LEGACY_THRESHOLDS
    elif exam_version == 5 and sorted(len(photo["boxes"]) for photo in gold.values()) == [0, 1, 1, 1, 2]:
        thresholds = EXAM_THRESHOLDS
    else:
        raise ValueError("Exam reference set does not match the selected version")
    results = []
    seen = set()
    completed = Counter()
    for aid, wid, state, outputs in attempts:
        if not aid or not wid or aid in seen:
            raise ValueError("Blank worker/assignment identifier or duplicate assignment in export")
        seen.add(aid)
        results.append(assess(gold, aid, wid, state, outputs, thresholds))
        if state in SCORABLE_STATES or state == "REJECTED":
            completed[wid] += 1
    for item in results:
        if not training and completed[item["worker_id"]] > 1:
            item["decision"] = "manual_review"
            item["issues"].append("Multiple attempts for the same worker in this export")
    return {"thresholds": thresholds, "attempts": results}


def score_assignments(assignments, gold, *, training=False, exam_version=5):
    """Grade the submitted outputs for a single pool, ignoring task hints and gold."""
    return score_attempts(api_attempts(assignments), gold, training=training, exam_version=exam_version)


def score(source, gold, *, training=False, exam_version=5):
    return score_attempts(read_attempts(source), gold, training=training, exam_version=exam_version)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path, help="TSV or JSON from a single BBOX pool")
    stage = parser.add_mutually_exclusive_group()
    stage.add_argument("--training", action="store_true", help="Assess the 15-photo practice page")
    stage.add_argument("--exam-version", type=int, choices=(5, 10), default=5,
                       help="Assess the current 5-photo or legacy 10-photo exam (default: 5)")
    parser.add_argument("--passed-ids", action="store_true",
                        help="Print one passing worker ID per line for the requester interface")
    args = parser.parse_args()
    try:
        gold_path = TRAINING_GOLD if args.training else {5: GOLD, 10: LEGACY_GOLD}[args.exam_version]
        report = score(args.export, json.loads(gold_path.read_text(encoding="utf-8")),
                       training=args.training, exam_version=args.exam_version)
    except (OSError, ValueError, KeyError, TypeError) as err:
        parser.exit(2, f"Cannot score exam: {err}\n")
    if args.passed_ids:
        for worker in sorted({a["worker_id"] for a in report["attempts"] if a["decision"] == "pass"}):
            print(worker)
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
