"""Run a chosen detector on a folder of images and write Contest submission JSONL."""

import argparse
import json
from pathlib import Path

from PIL import Image
from tqdm import tqdm

from project.evaluate import (
    MODELS,
    detector_predictions,
    load_model,
    load_prompt,
    parse_qwen_response,
    predict_qwen,
)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-m", "--model", required=True, choices=MODELS)
    parser.add_argument("--images", type=Path, required=True, help="Folder with <photo_id> image filenames")
    parser.add_argument("-o", "--output", type=Path, required=True, help="Submission JSONL path")
    parser.add_argument("--prompt", type=Path, help="Custom Markdown prompt (Qwen only)")
    parser.add_argument("--labels", nargs="+", help="Detector labels (default: butterfly moth)")
    parser.add_argument("--score-threshold", type=float, help="Detector cutoff (default: 0.1)")
    args = parser.parse_args()
    if args.model == "qwen":
        if args.labels is not None or args.score_threshold is not None:
            parser.error("--labels and --score-threshold are detector-only arguments")
    else:
        if args.prompt is not None:
            parser.error("--prompt is only supported with --model qwen")
        args.labels = args.labels if args.labels is not None else ["butterfly", "moth"]
        args.score_threshold = args.score_threshold if args.score_threshold is not None else 0.1
        if not 0 <= args.score_threshold <= 1:
            parser.error("--score-threshold must be in [0, 1]")
        if any(not label.strip() for label in args.labels):
            parser.error("--labels must not contain blank labels")
    return args


def submission_boxes(predictions):
    boxes = []
    for prediction in predictions:
        left, top, right, bottom = prediction["box"]
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            raise ValueError(f"Invalid submission box: {prediction['box']}")
        boxes.append({"left": left, "top": top, "width": right - left, "height": bottom - top})
    return boxes


def main():
    args = parse_args()
    images = sorted(
        (int(path.stem), path)
        for path in args.images.iterdir()
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )
    if len(images) != 400 or len({photo_id for photo_id, _ in images}) != 400:
        raise SystemExit("Expected 400 images with unique photo IDs in their filenames")

    prompt = load_prompt(args.prompt) if args.model == "qwen" else None
    pipe = load_model(args.model)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    log_path = args.output.with_suffix(".attempts.jsonl")
    results = []
    failed = 0
    # Append so manual reruns preserve previous failures and subsequent attempts.
    with log_path.open("a", encoding="utf-8") as log:
        for photo_id, path in tqdm(images, desc="Predicting", unit="image", ncols=120):
            attempt = {"photo_id": photo_id, "model": MODELS[args.model], "boxes": None, "error": None}
            try:
                with Image.open(path) as source:
                    image = source.convert("RGB")
                if prompt is not None:
                    attempt["response"] = predict_qwen(pipe, image, prompt)
                    predictions = parse_qwen_response(attempt["response"])
                else:
                    output = pipe(image, candidate_labels=args.labels, threshold=args.score_threshold)
                    predictions = detector_predictions(output, image.size)
                boxes = submission_boxes(predictions)
                attempt["boxes"] = boxes
                results.append({"photo_id": photo_id, "boxes": boxes, "error": None})
            except Exception as error:
                attempt["error"] = f"{type(error).__name__}: {error}"
                failed += 1
            log.write(json.dumps(attempt, allow_nan=False) + "\n")
            log.flush()

    print(f"Succeeded: {len(results)}; failed: {failed}. Attempt log: {log_path}")
    if failed:
        raise SystemExit("Submission not written; fix the failed items and rerun. Any existing output is unchanged.")

    content = "".join(json.dumps(row, allow_nan=False) + "\n" for row in results).encode("utf-8")
    if len(content) > 5 * 1024 * 1024:
        raise SystemExit("Submission exceeds 5 MiB; output not written")
    args.output.write_bytes(content)
    print(f"Wrote {len(results)} predictions to {args.output}")


if __name__ == "__main__":
    main()
