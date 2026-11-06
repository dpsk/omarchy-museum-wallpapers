#!/usr/bin/env python3
"""Fetch the data built into a release: for each `fame` level ("famous",
"known"), the painters and all their paintings on Wikidata, as
seed/fame-<level>.json.gz. The plugin starts from these, so its first painting
does not wait 30-60 s for Wikidata, and refreshes them in the background.

    python3 tools/seed.py [--out seed]    (or: make seed)

Run it by hand before a release; it needs only the network. Standard library
only."""

import argparse
import gzip
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("museum", ROOT / "museum.py")
museum = importlib.util.module_from_spec(spec)
spec.loader.exec_module(museum)

LEVELS = ("famous", "known")
# Less than this means Wikidata answered with a cut-off list.
MIN_PAINTERS = 100
MIN_PAINTINGS = 10000


def fetch(level, attempts, wait):
    for attempt in range(1, attempts + 1):
        try:
            bundle = museum.build_fame_bundle(level)
            if len(bundle["painters"]) < MIN_PAINTERS or len(bundle["items"]) < MIN_PAINTINGS:
                raise ValueError(f"only {len(bundle['painters'])} painters, {len(bundle['items'])} paintings")
            return bundle
        except Exception as exc:  # noqa: BLE001 - Wikidata is flaky under load; try again
            print(f"fame {level}, attempt {attempt}/{attempts}: {exc}", file=sys.stderr)
            if attempt < attempts:
                time.sleep(wait)
    raise SystemExit(f"could not fetch the painters and paintings for fame level {level}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=ROOT / "seed")
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--wait", type=float, default=30)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for name in LEVELS:
        level = museum.FAME[name]
        bundle = fetch(level, args.attempts, args.wait)
        path = args.out / f"fame-{level}.json.gz"
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as handle:
            json.dump(bundle, handle, ensure_ascii=False, separators=(",", ":"))
        tmp.replace(path)
        print(f"{name}: {len(bundle['painters'])} painters, {len(bundle['items'])} paintings, "
              f"{path.stat().st_size / 1e6:.2f} MB -> {path}")


if __name__ == "__main__":
    main()
