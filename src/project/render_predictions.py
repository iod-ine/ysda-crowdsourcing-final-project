import argparse
import json
import math
import shutil
from pathlib import Path

from PIL import Image, ImageDraw
from tqdm import tqdm


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Draw normalized prediction boxes on images.")
    parser.add_argument("-i", "--input", type=Path, default=Path("data/owl_prediction.jsonl"))
    parser.add_argument("--images", type=Path, default=Path("data/images/prediction"))
    parser.add_argument("-o", "--output", type=Path, default=Path("data/images/results"), help="Base output directory")
    parser.add_argument("--model", help="Model subfolder (default: input filename without _prediction.jsonl)")
    return parser.parse_args()


def draw_boxes(image: Image.Image, boxes: list[dict]) -> Image.Image:
    result = image.convert("RGB")
    draw = ImageDraw.Draw(result)
    width, height = result.size
    line_width = max(2, round(min(width, height) / 250))
    for box in boxes:
        left, top, box_width, box_height = (float(box[key]) for key in ("left", "top", "width", "height"))
        if not all(math.isfinite(value) for value in (left, top, box_width, box_height)):
            raise ValueError(f"Non-finite box: {box}")
        right, bottom = left + box_width, top + box_height
        if box_width <= 0 or box_height <= 0 or not (0 <= left < right <= 1 + 1e-9 and 0 <= top < bottom <= 1 + 1e-9):
            raise ValueError(f"Invalid normalized box: {box}")
        draw.rectangle(
            (left * (width - 1), top * (height - 1), min(right, 1) * (width - 1), min(bottom, 1) * (height - 1)),
            outline="red",
            width=line_width,
        )
    return result


def main():
    args = parse_args()
    model = args.model if args.model is not None else args.input.stem.removesuffix("_prediction")
    if not model or model in {".", ".."} or "/" in model or "\\" in model:
        raise ValueError("Model must be a nonempty folder name, not a path")
    output_dir = args.output / model
    if args.images.resolve() == output_dir.resolve():
        raise ValueError("Output directory must differ from the source images directory")
    with args.input.open(encoding="utf-8") as source:
        predictions = [json.loads(line) for line in source if line.strip()]

    images = {}
    for path in args.images.iterdir():
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
            if path.stem in images:
                raise ValueError(f"Multiple images for photo_id {path.stem}")
            images[path.stem] = path

    output_dir.mkdir(parents=True, exist_ok=True)
    saved = empty = failed = 0
    for prediction in tqdm(predictions, desc="Rendering predictions", unit="image", ncols=120):
        photo_id = prediction["photo_id"]
        try:
            if prediction.get("error"):
                raise ValueError(prediction["error"])
            source = images[str(photo_id)]
            destination = output_dir / source.name
            boxes = prediction["boxes"]
            if boxes:
                with Image.open(source) as image:
                    draw_boxes(image, boxes).save(destination)
            else:
                shutil.copy2(source, destination)
                empty += 1
            saved += 1
        except (KeyError, TypeError, ValueError, OSError) as error:
            tqdm.write(f"Failed photo_id={photo_id}: {error}")
            failed += 1

    print(f"Saved {saved} images to {output_dir} ({empty} without boxes); {failed} failed.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
