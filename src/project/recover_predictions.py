"""Recover Qwen predictions from saved responses without rerunning the model."""

import argparse
import json
import math
import re
from pathlib import Path

from project.evaluate import parse_qwen_response
from project.predict import submission_boxes


def recover_response(response):
    text = response.strip()
    lines = text.splitlines()
    if len(lines) >= 3 and lines[0] in ("```json", "```") and lines[-1] == "```":
        text = "\n".join(lines[1:-1]).strip()
    repairs = []
    try:
        output = json.loads(text)
    except json.JSONDecodeError:
        # Accept only the observed single-box structure with a missing outer ]
        # and either one or two closing braces. Never extract arbitrary JSON.
        match = re.fullmatch(r'\{\s*"boxes"\s*:\s*\[\s*(\[[^\[\]{}]*\])\s*\}\s*\}?', text)
        if match is None:
            raise ValueError("Response does not match a supported closing-bracket repair")
        output = {"boxes": [json.loads(match.group(1))]}
        repairs.append("corrected_single_box_closing_brackets")

    if not isinstance(output, dict) or set(output) != {"boxes"} or not isinstance(output["boxes"], list):
        raise ValueError('Expected an object containing only a "boxes" array')
    boxes = output["boxes"]
    if len(boxes) == 4 and all(type(value) in (int, float) for value in boxes):
        boxes = [boxes]
        output["boxes"] = boxes
        repairs.append("wrapped_flat_single_box")
    for box in boxes:
        if not isinstance(box, list) or len(box) != 4:
            raise ValueError(f"Expected four coordinates per box: {box}")
        if not all(type(value) in (int, float) and math.isfinite(value) for value in box):
            raise ValueError(f"Invalid coordinates: {box}")
        x1, y1, x2, y2 = box
        if not (0 <= x1 < x2 <= 1000 and 0 <= y1 < y2 <= 1000):
            raise ValueError(f"Invalid relative box: {box}")
    corrected = json.dumps(output, allow_nan=False)
    return submission_boxes(parse_qwen_response(corrected)), repairs, corrected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-i", "--input", type=Path, default=Path("data/qwen_prediction.attempts.jsonl"))
    parser.add_argument("-o", "--output", type=Path, default=Path("data/qwen_prediction.jsonl"))
    parser.add_argument("--log", type=Path, help="Recovery audit path (default: <output stem>.recovery.jsonl)")
    args = parser.parse_args()
    log_path = args.log if args.log is not None else args.output.with_suffix(".recovery.jsonl")
    if len({path.resolve() for path in (args.input, args.output, log_path)}) != 3:
        parser.error("Input, output, and recovery log must be different files")

    # An appended attempt log may contain reruns: use the latest attempt per ID.
    attempts = {}
    with args.input.open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                attempt = json.loads(line)
                attempts[attempt["photo_id"]] = attempt

    results, audit = [], []
    failures = repaired = 0
    for photo_id, attempt in attempts.items():
        entry = {"photo_id": photo_id, "original_error": attempt.get("error")}
        try:
            boxes, repairs, corrected = recover_response(attempt["response"])
            results.append({"photo_id": photo_id, "boxes": boxes, "error": None})
            entry.update(repairs=repairs, corrected_response=corrected, error=None)
            repaired += bool(repairs)
        except (KeyError, TypeError, ValueError) as error:
            entry["error"] = f"{type(error).__name__}: {error}"
            failures += 1
        audit.append(entry)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("".join(json.dumps(row, allow_nan=False) + "\n" for row in audit), encoding="utf-8")
    if failures:
        raise SystemExit(f"{failures} unrecoverable responses; output unchanged. See {log_path}")
    if not results:
        raise SystemExit("No attempts found; output unchanged")
    content = "".join(json.dumps(row, allow_nan=False) + "\n" for row in results)
    if len(content.encode("utf-8")) > 5 * 1024 * 1024:
        raise SystemExit("Submission exceeds 5 MiB; output unchanged")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")
    print(f"Wrote {len(results)} predictions to {args.output}; repaired {repaired} responses.")
    print(f"Recovery audit: {log_path}. Original attempts log unchanged.")


if __name__ == "__main__":
    main()
