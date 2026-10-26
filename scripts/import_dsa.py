"""Import the v1 DSA trainer as the curated `dsa` track (BE-09, decisions D2, D7).

Reads a Jolt checkout — the problem batches in `data/archived/` (and
`data/new/` with `--include-new`, only problems whose `descriptionStatus` is
`original`), the pattern library and the decision tree from
`src/tracks/dsa/content/` — validates it like Jolt's `scripts/check-batch.mjs`
and upserts it into Cosmos by `leetcodeId`. Idempotent: a second run with the
same input writes nothing.

    python scripts/import_dsa.py --from ../Jolt --dry-run
    python scripts/import_dsa.py --from ../Jolt
    python scripts/import_dsa.py --from ../Jolt --include-new
    python scripts/import_dsa.py --from ../Jolt --content dsa-content.json   # npm run export:dsa

Target: the Cosmos account in the environment / `.env` (COSMOS_ENDPOINT, AUTH_MODE,
COSMOS_KEY …; see docs/DEPLOY.md). With STORAGE_MODE=memory it imports into a
throwaway in-memory store, which is only useful as a full validation run (or use
DSA_SEED_FROM to seed a memory-mode server at startup).

Options:
  --include-new   also import data/new/*.json (only problems marked original)
  --dry-run       validate and report what would change; write nothing
  --prune         retire problems that are no longer in the input
  --content FILE  library + decision tree from a JSON export (`npm run export:dsa`
                  in Jolt, or scripts/export_dsa_from_jolt.mjs) instead of reading
                  the TS sources directly
  --json          print the report as JSON
  --verbose       list every warning

Exit codes: 0 done (or nothing to do), 1 validation errors (nothing written),
2 the checkout is missing a file or does not parse.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _parse(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from", dest="repo", required=True, help="path to the Jolt repo")
    ap.add_argument("--include-new", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--content", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    return ap.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    from jolt.config import get_settings
    from jolt.curated.dsa_import import ImportRefused, import_dsa
    from jolt.curated.dsa_source import SourceError, load_bundle
    from jolt.runtime import Runtime

    try:
        bundle = load_bundle(args.repo, include_new=args.include_new, content_json=args.content)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if bundle.review:
        print(f"{len(bundle.review)} to review — a word that names the problem's own tag:")
        for r in bundle.review:
            print(f"  ! {r}")
    print(f"{len(bundle.warnings)} warning(s){'' if args.verbose else ' (--verbose lists them)'}")
    if args.verbose:
        for w in bundle.warnings:
            print(f"  ~ {w}")
    if bundle.errors:
        print(f"\n{len(bundle.errors)} error(s); nothing written:")
        for e in bundle.errors:
            print(f"  ✗ {e}")
        return 1

    settings = get_settings()
    rt = await Runtime.create(settings)
    try:
        report = await import_dsa(rt, bundle, dry_run=args.dry_run, prune=args.prune)
    except ImportRefused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await rt.close()

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
        return 0
    verb = "would change" if args.dry_run else "changed"
    print(
        f"\n{'DRY RUN — ' if args.dry_run else ''}track {report.track_id} "
        f"({settings.storage_mode}): {', '.join(report.batches)}"
    )
    print(
        f"  problems: {report.playable} playable, {report.skipped} skipped (not original yet); "
        + ", ".join(f"{k} {v}" for k, v in sorted(report.by_difficulty.items()))
    )
    print(f"  track: {report.track}; content (decision tree, index): {report.content}")
    print("  concepts: " + ", ".join(f"{k} {v}" for k, v in report.concepts.as_dict().items()))
    print("  questions: " + ", ".join(f"{k} {v}" for k, v in report.questions.as_dict().items()))
    print(f"\n{verb.capitalize()}: {'yes' if report.changed else 'nothing'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
