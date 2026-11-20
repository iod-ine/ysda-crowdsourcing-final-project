"""Evaluate local butterfly/moth detections with greedy one-to-one IoU matching."""

import argparse
import csv
import json
import math
import time
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image
from tqdm import tqdm
from transformers import pipeline

MODELS = {
    "dino": "IDEA-Research/grounding-dino-tiny",
    "owl": "google/owlv2-base-patch16",
    "qwen": "Qwen/Qwen3-VL-2B-Instruct",
}
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
CSV_FIELDS = [
    "photo_id",
    "model",
    "labels",
    "iou_threshold",
    "score_threshold",
    "status",
    "error",
    "tp",
    "fp",
    "fn",
    "precision",
    "recall",
    "f1",
    "mean_matched_iou",
    "gt_boxes",
    "pred_boxes",
    "inference_seconds",
]


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-m", "--model", required=True, choices=MODELS)
    parser.add_argument("-i", "--input", type=Path, default=Path("data/golden_set.json"))
    parser.add_argument("--images", type=Path, default=Path("data/images"))
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--iou-threshold", type=float, default=0.5)
    parser.add_argument("--score-threshold", type=float, help="Detector cutoff (default: 0.1)")
    parser.add_argument("--labels", nargs="+", help="Detector labels (default: butterfly moth)")
    parser.add_argument("--prompt", type=Path, help="Custom Markdown prompt (Qwen only)")
    args = parser.parse_args()
    if not 0 < args.iou_threshold <= 1:
        parser.error("--iou-threshold must be in (0, 1]")
    if args.score_threshold is not None and (not 0 <= args.score_threshold <= 1):
        parser.error("--score-threshold must be in [0, 1]")
    if args.labels is not None and any(not label.strip() for label in args.labels):
        parser.error("--labels must not contain blank labels")
    if args.model == "qwen":
        if args.labels is not None or args.score_threshold is not None:
            parser.error("--labels and --score-threshold are detector-only arguments")
    else:
        if args.prompt is not None:
            parser.error("--prompt is only supported with --model qwen")
        args.labels = args.labels if args.labels is not None else ["butterfly", "moth"]
        args.score_threshold = args.score_threshold if args.score_threshold is not None else 0.1
    return args


def load_model(model):
    if model == "qwen":
        return pipeline("image-text-to-text", model=MODELS[model], dtype="auto")
    return pipeline("zero-shot-object-detection", model=MODELS[model])


def load_prompt(path=None):
    path = path if path is not None else PROMPTS_DIR / "qwen.md"
    wording = path.read_text(encoding="utf-8")
    if not wording.strip():
        raise ValueError("Qwen prompt must not be empty")
    rules = (PROMPTS_DIR / "qwen_rules.md").read_text(encoding="utf-8")
    return wording.rstrip() + "\n\n" + rules


def predict_qwen(pipe, image, prompt):
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": prompt},
            ],
        }
    ]
    output = pipe(
        text=messages,
        max_new_tokens=1024,
        generate_kwargs={"do_sample": False},
        return_full_text=False,
    )
    response = output[0]["generated_text"]
    if not isinstance(response, str):
        raise TypeError("Qwen pipeline did not return response text")
    return response


def parse_qwen_response(response):
    lines = response.strip().splitlines()
    if len(lines) >= 3 and lines[0] in ("```json", "```") and lines[-1] == "```":
        response = "\n".join(lines[1:-1])
    output = json.loads(response)
    if not isinstance(output, dict) or set(output) != {"boxes"} or not isinstance(output["boxes"], list):
        raise ValueError('Qwen response must be a JSON object with a "boxes" array only')
    return [{"box": normalize_box(box, 1000, 1000)} for box in output["boxes"]]


def image_path(item, directory):
    return directory / f"{item['photo_id']}{Path(urlsplit(item['image']).path).suffix}"


def normalize_box(box, width=1, height=1, *, clip=False):
    x1, y1, x2, y2 = box
    normalized = [x1 / width, y1 / height, x2 / width, y2 / height]
    if clip:
        normalized = [min(1.0, max(0.0, x)) for x in normalized]
    return normalized


def golden_boxes(item):
    status, rectangles = item["status"], item["result"]
    if status == "empty":
        return []
    boxes = []
    for rect in rectangles:
        left, top, width, height = [rect[key] for key in ("left", "top", "width", "height")]
        boxes.append(normalize_box([left, top, left + width, top + height]))
    return boxes


def detector_predictions(output, size):
    predictions = []
    for detection in output:
        box = [detection["box"][key] for key in ("xmin", "ymin", "xmax", "ymax")]
        prediction = {"box": normalize_box(box, *size, clip=True)}
        if "score" in detection:
            prediction["score"] = detection["score"]
        if "label" in detection:
            prediction["label"] = detection["label"]
        predictions.append(prediction)
    return predictions


def calculate_iou(a, b):
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area_a = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    area_b = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    union = area_a + area_b - intersection
    return intersection / union if union else 0.0


def count_metrics(tp, fp, fn):
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
    }


def evaluate_boxes(ground_truth, predictions, threshold=0.5):
    """Greedily take the highest-IoU remaining pair; never reuse either box."""
    pairs = sorted(
        ((calculate_iou(gt, pred), g, p) for g, gt in enumerate(ground_truth) for p, pred in enumerate(predictions)),
        reverse=True,
    )
    matched_gt, matched_pred, ious = set(), set(), []
    for iou, g, p in pairs:
        if iou < threshold:
            break
        if g not in matched_gt and p not in matched_pred:
            matched_gt.add(g)
            matched_pred.add(p)
            ious.append(iou)
    tp = len(ious)
    metrics = count_metrics(tp, len(predictions) - tp, len(ground_truth) - tp)
    metrics["mean_matched_iou"] = sum(ious) / tp if tp else None
    return metrics


def print_summary(totals, succeeded, failed, empty, false_empty):
    metrics = count_metrics(**totals)
    print(f"Succeeded: {succeeded}; failed: {failed}")
    print("; ".join(f"{name.upper()}: {value}" for name, value in totals.items()))
    for name in ("precision", "recall", "f1"):
        value = metrics[name]
        print(f"{name}: {value:.4f}" if value is not None else f"{name}: undefined")
    fraction = f"{false_empty / empty:.4f}" if empty else "undefined"
    print(f"Empty images with false detections: {false_empty}/{empty} ({fraction})")


def main():
    args = parse_args()
    try:
        data = json.loads(args.input.read_text(encoding="utf-8"))
        assert "items" in data
    except (OSError, ValueError) as error:
        raise SystemExit(f"Cannot load golden set {args.input}: {error}") from error

    try:
        prompt = load_prompt(args.prompt) if args.model == "qwen" else None
    except (OSError, ValueError) as error:
        raise SystemExit(f"Cannot load Qwen prompt {args.prompt}: {error}") from error

    pipe = load_model(args.model)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if prompt is not None:
        args.output.with_suffix(".prompt.md").write_text(prompt, encoding="utf-8")
    totals = {"tp": 0, "fp": 0, "fn": 0}
    succeeded = failed = empty = false_empty = 0
    with (
        args.output.open("w", newline="", encoding="utf-8") as destination,
        (
            args.output.with_suffix(".responses.jsonl").open("w", encoding="utf-8")
            if prompt is not None
            else nullcontext()
        ) as responses,
    ):
        writer = csv.DictWriter(destination, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for item in tqdm(data["items"], desc="Evaluating", unit="image", ncols=120):
            row = {
                "photo_id": item.get("photo_id", "") if isinstance(item, dict) else "",
                "model": MODELS[args.model],
                "labels": json.dumps(args.labels) if args.labels is not None else None,
                "iou_threshold": args.iou_threshold,
                "score_threshold": args.score_threshold,
            }
            try:
                gt = golden_boxes(item)
                row["gt_boxes"] = json.dumps(gt)
                with Image.open(image_path(item, args.images)) as source:
                    image = source.convert("RGB")
                start = time.perf_counter()
                try:
                    if prompt is not None:
                        output = predict_qwen(pipe, image, prompt)
                    else:
                        output = pipe(
                            image,
                            candidate_labels=args.labels,
                            threshold=args.score_threshold,
                        )
                finally:
                    row["inference_seconds"] = time.perf_counter() - start
                if responses is not None:
                    responses.write(json.dumps({"photo_id": item["photo_id"], "response": output}) + "\n")
                    responses.flush()
                    predictions = parse_qwen_response(output)
                else:
                    predictions = detector_predictions(output, image.size)
                row["pred_boxes"] = json.dumps(predictions)
                metrics = evaluate_boxes(gt, [p["box"] for p in predictions], args.iou_threshold)
                row.update(status="ok", **metrics)
                for key in totals:
                    totals[key] += metrics[key]
                succeeded += 1
                if not gt:
                    empty += 1
                    false_empty += bool(predictions)
            except Exception as error:
                row.update(status="error", error=f"{type(error).__name__}: {error}")
                failed += 1
            writer.writerow(row)
            destination.flush()
    print_summary(totals, succeeded, failed, empty, false_empty)


if __name__ == "__main__":
    main()
