import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

import requests
from tqdm import tqdm


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Materialize images for evaluation.")
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        default=Path("data/golden_set.json"),
        help="Path to the golden set (default: data/golden_set.json)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("data/images"),
        help="Directory for downloaded images (default: data/images)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    with Path(args.input).open(encoding="utf-8") as source:
        items = json.load(source)["items"]

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    for item in tqdm(items, desc="Downloading images", unit="image", ncols=120):
        url = item["image"]
        extension = Path(urlsplit(url).path).suffix
        destination = output_dir / f"{item['photo_id']}{extension}"
        if destination.exists():
            continue

        response = requests.get(url, timeout=60)
        response.raise_for_status()
        destination.write_bytes(response.content)


if __name__ == "__main__":
    main()
