"""
Guard used by the daily refresh workflow (.github/workflows/daily-refresh.yml)
before it commits/pushes freshly fetched data: rebuilds the DB from
data/seed/*.csv and fails loudly if load_db.py had to skip an unusual
number of rows for unresolved foreign keys.

A handful of skips can be normal (e.g. a brand-new call-up not yet on
any roster snapshot); a large fraction skipped is the signature of the
real corruption bug this project hit once already (an old-format CSV
silently getting its columns misaligned by --append) — see
check_header_compatible() in fetch_data.py, which is the primary
defense against that. This is a second, independent check: even if
something slips past that guard, don't let a corrupted dataset get
auto-committed and pushed to the live app.

Usage:
    python3 scripts/ci_sanity_check.py [--max-skip-fraction 0.01]
Exit code is non-zero (and prints why) if the check fails.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-skip-fraction", type=float, default=0.01,
                         help="fail if skipped/loaded exceeds this fraction for any table (default 1%%)")
    args = parser.parse_args()

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "load_db.py")],
        cwd=ROOT, capture_output=True, text=True,
    )
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        sys.exit(f"load_db.py failed (exit {result.returncode}) — aborting before commit.")

    # Lines look like: "  loaded   52737 rows into player_game_logs  (26549 skipped: unresolved foreign key)"
    problems = []
    for line in result.stdout.splitlines():
        m = re.search(r"loaded\s+(\d+)\s+rows into (\w+)(?:\s+\((\d+) skipped)?", line)
        if not m:
            continue
        loaded, table, skipped = int(m.group(1)), m.group(2), int(m.group(3) or 0)
        total = loaded + skipped
        if total > 0 and skipped / total > args.max_skip_fraction:
            problems.append(f"  {table}: {skipped}/{total} rows skipped ({skipped/total:.1%}) — over the {args.max_skip_fraction:.1%} threshold")

    if problems:
        print("\nSanity check FAILED — an unusually large fraction of rows were skipped:")
        for p in problems:
            print(p)
        print("\nThis usually means a CSV got corrupted (e.g. mismatched columns from a "
              "bad merge). Refusing to commit/push this data — investigate data/seed/*.csv "
              "before re-running.")
        sys.exit(1)

    print("Sanity check passed.")


if __name__ == "__main__":
    main()
