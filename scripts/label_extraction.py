#!/usr/bin/env python3
"""Stratified sampler over the LLM extraction archive — the picking half of
the assisted-labeling flow. The labeling itself is interactive (Claude
pre-labels, Harsh verifies in chat); accepted rows land in
evals/golden_extraction.jsonl, NOT here.

Usage:
    uv run python scripts/label_extraction.py stats  --archive-dir PATH
    uv run python scripts/label_extraction.py sample --archive-dir PATH --n 60 --out PATH

Sampling is deterministic for a given archive (no randomness): rows from every
*.jsonl in the archive dir are sorted by archived_at, grouped into buckets by
their model-claims' primary relation (the first claim's relation, or
ZERO_CLAIMS when the model emitted none), then drawn round-robin across the
buckets in alphabetical order until n rows are picked. Each sampled row is
written as its raw archive line plus a generated "id" field (X01..Xnn).

Stdlib only.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path


def load_archive(archive_dir: Path) -> list[dict]:
    """All parseable rows from every *.jsonl in archive_dir, sorted by
    archived_at (ties keep file order — files are read in sorted-name order,
    so the result is deterministic for the same inputs). Unparseable lines
    (e.g. a torn tail line) are skipped with a stderr notice."""
    rows: list[dict] = []
    skipped = 0
    for path in sorted(archive_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                skipped += 1
    if skipped:
        print(f"skipped {skipped} unparseable line(s)", file=sys.stderr)
    rows.sort(key=lambda row: str(row.get("archived_at", "")))
    return rows


def bucket_of(row: dict) -> str:
    """Stratification bucket: the first model claim's relation (uppercased),
    or ZERO_CLAIMS for rows where the model emitted no claims — genuine
    negatives are labeling gold and deserve their own bucket."""
    claims = row.get("claims") or []
    if not claims:
        return "ZERO_CLAIMS"
    relation = str(claims[0].get("relation", "")).strip().upper()
    return relation or "UNKNOWN"


def stratify(rows: list[dict]) -> dict[str, list[dict]]:
    """Group rows by bucket_of, preserving row order within each bucket."""
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        buckets.setdefault(bucket_of(row), []).append(row)
    return buckets


def round_robin_sample(buckets: dict[str, list[dict]], n: int) -> list[dict]:
    """Draw one row per non-empty bucket per round until n rows are picked or
    the archive is exhausted.

    Buckets are visited in a DETERMINISTIC HASH order, not alphabetically:
    with free-text relations the bucket count can exceed n, and an
    alphabetical visit order would then give every late-alphabet relation
    (TRAVELED_TO, WARNED, ...) zero representation on every run — a bias hit
    twice in practice before this ordering. sha256 of the bucket name spreads
    the cutoff uniformly across the alphabet while staying reproducible."""
    order = sorted(buckets, key=lambda name: hashlib.sha256(name.encode()).hexdigest())
    if n < sum(1 for name in order if buckets[name]):
        print(
            f"note: n={n} is below the non-empty bucket count "
            f"({sum(1 for b in buckets.values() if b)}); some relation buckets "
            "will be unrepresented (hash order keeps the cut unbiased).",
            file=sys.stderr,
        )
    queues = {name: list(buckets[name]) for name in order}
    picked: list[dict] = []
    while len(picked) < n and any(queues.values()):
        for queue in queues.values():
            if not queue:
                continue
            picked.append(queue.pop(0))
            if len(picked) == n:
                break
    return picked


def cmd_stats(args: argparse.Namespace) -> int:
    rows = load_archive(args.archive_dir)
    buckets = stratify(rows)
    print(f"{len(rows)} rows across {len(buckets)} buckets in {args.archive_dir}")
    for name, bucket in sorted(buckets.items(), key=lambda item: (-len(item[1]), item[0])):
        print(f"{len(bucket):5d}  {name}")
    return 0


def cmd_sample(args: argparse.Namespace) -> int:
    rows = load_archive(args.archive_dir)
    if not rows:
        print(f"no archive rows found in {args.archive_dir}", file=sys.stderr)
        return 1
    buckets = stratify(rows)
    sampled = round_robin_sample(buckets, args.n)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for i, row in enumerate(sampled, start=1):
            out_row = {"id": f"X{i:02d}"}
            out_row.update((key, value) for key, value in row.items() if key != "id")
            handle.write(json.dumps(out_row) + "\n")
    print(f"wrote {len(sampled)} of {len(rows)} rows ({len(buckets)} buckets) to {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="label_extraction.py",
        description="Deterministic stratified sampler over the LLM extraction archive.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    stats = subparsers.add_parser("stats", help="print bucket counts")
    stats.add_argument("--archive-dir", type=Path, required=True)
    stats.set_defaults(func=cmd_stats)

    sample = subparsers.add_parser("sample", help="write n stratified rows to --out")
    sample.add_argument("--archive-dir", type=Path, required=True)
    sample.add_argument("--n", type=int, default=60)
    sample.add_argument("--out", type=Path, required=True)
    sample.set_defaults(func=cmd_sample)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
