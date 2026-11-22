import argparse
import csv
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

API = "https://api.inaturalist.org/v1/observations"
FAMILIES = {
    "Papilionidae": 47223,
    "Pieridae": 48508,
    "Nymphalidae": 47922,
    "Lycaenidae": 47923,
    "Hesperiidae": 47653,
    "Riodinidae": 59166,
    "Hedylidae": 325456,
}
GROUPS = {
    "butterflies": (300, FAMILIES),
    "insects/bees": (25, {"Anthophila": 630955}),
    "insects/moths": (35, {"Erebidae": 121850, "Geometridae": 49530, "Noctuidae": 48670}),
    "insects/other": (20, {"Syrphidae": 49995, "Coccinellidae": 48486}),
    "insects/caterpillars": (20, {"Lepidoptera": 47157}),
    "plants": (60, {"Asteraceae": 47604, "Rosaceae": 47148}),
}
MULTIPLE_LIMIT = 40
MULTIPLE_QUERIES = ("three butterflies", "two butterflies", "in copula", "mating pair", "mating")
MULTIPLE_HINT = re.compile(
    r"\b(?:(?:two|three|2|3|several) (?:adult )?butterflies|"
    r"in copula|copulating|mating (?:pair|couple)|"
    r"(?:butterflies|pair|they|observed|found|seen) (?:were |are |them )?mating)\b",
    re.IGNORECASE,
)
FIELDS = [
    "photo_id", "image_url", "source_taxon", "source_taxon_id",
    "taxon", "taxon_id", "observation_id", "observation_url",
    "observer", "photo_attribution", "license", "search_query", "description",
]


def observations(taxon_id, page, **filters):
    params = urlencode({
        "taxon_id": taxon_id,
        "photos": "true",
        "photo_license": "cc0",
        "per_page": 80,
        "page": page,
        "order_by": "id",
        "order": "desc",
        **filters,
    })
    request = Request(f"{API}?{params}", headers={"User-Agent": "ButterflyCourseProject/2.0"})
    for attempt in range(4):
        try:
            with urlopen(request, timeout=35) as response:
                data = json.load(response)
            time.sleep(1)
            return data
        except (URLError, TimeoutError) as error:
            if isinstance(error, HTTPError) and error.code not in {429, 500, 502, 503, 504}:
                raise
            if attempt == 3:
                raise
            retry_after = error.headers.get("Retry-After", "") if isinstance(error, HTTPError) else ""
            time.sleep(int(retry_after) if retry_after.isdecimal() else 2 ** attempt)


def cc0_photos(source_name, source_id, *, query="", larva=False):
    filters = {}
    if query:
        filters.update(q=query, search_on="description", quality_grade="research")
    if larva:
        filters.update(term_id=1, term_value_id=6)
    page = 1
    while True:
        data = observations(source_id, page, **filters)
        for observation in data["results"]:
            description = " ".join((observation.get("description") or "").split())
            if query and not MULTIPLE_HINT.search(description):
                continue
            taxon = observation.get("taxon") or {}
            user = observation.get("user") or {}
            for photo in observation.get("photos", []):
                url = urlsplit(photo.get("url") or "")
                if (photo.get("license_code") != "cc0" or photo.get("hidden")
                        or url.scheme != "https" or url.netloc != "inaturalist-open-data.s3.amazonaws.com"
                        or not url.path.startswith(f"/photos/{photo['id']}/square.")):
                    continue
                original = url._replace(path=url.path.replace("/square.", "/original.", 1)).geturl()
                yield {
                    "photo_id": photo["id"],
                    "image_url": original,
                    "source_taxon": source_name,
                    "source_taxon_id": source_id,
                    "taxon": taxon.get("name", ""),
                    "taxon_id": taxon.get("id", ""),
                    "observation_id": observation["id"],
                    "observation_url": f"https://www.inaturalist.org/observations/{observation['id']}",
                    "observer": user.get("login", ""),
                    "photo_attribution": photo.get("attribution") or "",
                    "license": photo["license_code"],
                    "search_query": query,
                    "description": description if query else "",
                }
                break
        if not data["results"] or page * data["per_page"] >= data["total_results"]:
            return
        page += 1


def collect(sources, limit, seen_photos, seen_observations):
    by_source = {name: [] for name in sources}
    count = 0
    while sources:
        for name, photos in list(sources.items()):
            for row in photos:
                photo_id, observation_id = row["photo_id"], row["observation_id"]
                if photo_id in seen_photos or observation_id in seen_observations:
                    continue
                by_source[name].append(row)
                seen_photos.add(photo_id)
                seen_observations.add(observation_id)
                count += 1
                break
            else:
                del sources[name]
            if count == limit:
                return by_source
    raise RuntimeError(f"Найдено только {count} уникальных ссылок из {limit}.")


def write_links(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tsv.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)
    print(f"{path}: {len(rows)}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("inat_links"), help="Папка с TSV.")
    args = parser.parse_args()
    seen_photos, seen_observations = set(), set()
    try:
        print("Ищу кадры с несколькими бабочками по описаниям…", flush=True)
        sources = {query: cc0_photos("Papilionoidea", 47224, query=query) for query in MULTIPLE_QUERIES}
        multiple = collect(sources, MULTIPLE_LIMIT, seen_photos, seen_observations)
        results = {
            args.out / "multiple_butterflies_candidates" / "links.tsv":
                [row for rows in multiple.values() for row in rows]
        }
        for group, (limit, taxa) in GROUPS.items():
            print(f"Собираю {group}…", flush=True)
            sources = {
                name: cc0_photos(name, taxon_id, larva=group == "insects/caterpillars")
                for name, taxon_id in taxa.items()
            }
            for name, rows in collect(sources, limit, seen_photos, seen_observations).items():
                results[args.out / group / name / "links.tsv"] = rows
        for path, rows in results.items():
            write_links(path, rows)
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"Ошибка: {error}\n")
    print(f"Всего: {len(seen_photos)}. Папка: {args.out.resolve()}")


if __name__ == "__main__":
    main()
