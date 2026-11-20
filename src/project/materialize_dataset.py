import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

import requests
from tqdm import tqdm


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Materialize images from a JSONL dataset.")
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        default=Path("data/dataset.jsonl"),
        help="Path to the JSONL dataset (default: data/dataset.jsonl)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("data/images/prediction"),
        help="Directory for downloaded images (default: data/images/prediction)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    with args.input.open(encoding="utf-8") as source:
        items = [json.loads(line) for line in source if line.strip()]

    output_dir = args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    for item in tqdm(items, desc="Downloading images", unit="image", ncols=120):
        url = item["photo_url"]
        extension = Path(urlsplit(url).path).suffix
        destination = output_dir / f"{item['photo_id']}{extension}"
        if destination.exists():
            continue

        response = requests.get(url, timeout=60)
        response.raise_for_status()
        destination.write_bytes(response.content)


if __name__ == "__main__":
    main()
