"""Recover malformed Qwen evaluation responses without rerunning inference.

Only complete JSON objects or complete box lists with broken closing punctuation
are accepted. Truncated responses remain errors; coordinates are never invented
or deduplicated. The original evaluation and raw responses are not modified.
"""

import argparse
import csv
import json
import re
from pathlib import Path

from project.evaluate import evaluate_boxes, print_summary


BOX_OBJECT = re.compile(r'\{\s*"boxes"\s*:')
# A complete list of four-number boxes, followed only by closing punctuation.
NUMBER = r"-?\d+(?:\.\d+)?"
BOX = rf"\[\s*{NUMBER}\s*,\s*{NUMBER}\s*,\s*{NUMBER}\s*,\s*{NUMBER}\s*\]"
BROKEN_OBJECT = re.compile(
    rf'\{{\s*"boxes"\s*:\s*\[\s*(?P<boxes>{BOX}(?:\s*,\s*{BOX})*)\s*[\]\}}]+\s*'
)


def recover_boxes(response):
    text = response.strip()
    if text.startswith("```json"):
        text = text[len("```json"):].strip()
    if text.endswith("```"):
        text = text[:-3].strip()

    # One response contains commentary followed by a complete, valid object.
    objects = []
    for match in BOX_OBJECT.finditer(text):
        try:
            value, _ = json.JSONDecoder().raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and set(value) == {"boxes"}:
            objects.append(value["boxes"])
    if objects:
        if any(boxes != objects[0] for boxes in objects[1:]):
            raise ValueError("Conflicting complete box objects")
        boxes = objects[0]
    else:
        match = BROKEN_OBJECT.fullmatch(text)
        if match is None:
            raise ValueError("Response is truncated or cannot be repaired safely")
        boxes = json.loads("[" + match["boxes"] + "]")

    if not isinstance(boxes, list):
        raise ValueError("Boxes must be a list")
    for box in boxes:
        if (
            not isinstance(box, list)
            or len(box) != 4
            or any(type(x) not in (int, float) or not 0 <= x <= 1000 for x in box)
            or box[0] >= box[2]
            or box[1] >= box[3]
        ):
            raise ValueError(f"Invalid box: {box!r}")
    return [{"box": [x / 1000 for x in box]} for box in boxes]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("responses", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.resolve() in {args.csv.resolve(), args.responses.resolve()}:
        parser.error("Output must not overwrite either input")
    with args.csv.open(newline="") as source:
        reader = csv.DictReader(source)
        fields = reader.fieldnames + ["recovery_note"]
        rows = list(reader)
    responses = {}
    for line in args.responses.read_text().splitlines():
        item = json.loads(line)
        key = str(item["photo_id"])
        if key in responses:
            raise ValueError(f"Duplicate response for {key}")
        responses[key] = item["response"]

    recovered = 0
    for row in rows:
        row["recovery_note"] = ""
        if row["status"] == "ok":
            continue
        try:
            predictions = recover_boxes(responses[row["photo_id"]])
        except (KeyError, ValueError) as error:
            row["recovery_note"] = str(error)
            continue
        row["recovery_note"] = "Recovered from raw response; original error: " + row["error"]
        metrics = evaluate_boxes(
            json.loads(row["gt_boxes"]),
            [p["box"] for p in predictions],
            float(row["iou_threshold"]),
        )
        row.update(status="ok", error="", pred_boxes=json.dumps(predictions), **metrics)
        recovered += 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    ok = [row for row in rows if row["status"] == "ok"]
    totals = {key: sum(int(row[key]) for row in ok) for key in ("tp", "fp", "fn")}
    empty = [row for row in ok if not json.loads(row["gt_boxes"])]
    print(f"Recovered {recovered} rows; saved {args.output}")
    print_summary(totals, len(ok), len(rows) - len(ok), len(empty),
                  sum(bool(json.loads(row["pred_boxes"])) for row in empty))


if __name__ == "__main__":
    main()
