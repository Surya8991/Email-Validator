"""Refresh the disposable-domain snapshot used by LocalProvider.

Pulls the upstream `disposable/disposable-email-domains` list (~110k
entries — ~30x larger than the pypi `disposable-email-domains` pkg)
and writes it to app/data/disposable.txt. LocalProvider prefers this
snapshot over the pypi package on import.

Usage: python scripts/fetch_disposable.py
Idempotent — overwrites app/data/disposable.txt with the current list.
"""

import sys
import urllib.request
from pathlib import Path

SOURCE_URL = (
    "https://raw.githubusercontent.com/disposable/"
    "disposable-email-domains/master/domains.txt"
)
DEST = Path(__file__).resolve().parent.parent / "app" / "data" / "disposable.txt"
MIN_ENTRIES = 1000  # sanity guard against truncated / redirected downloads


def main() -> int:
    with urllib.request.urlopen(SOURCE_URL, timeout=60) as resp:
        body = resp.read().decode("utf-8")
    lines = [
        ln.strip().lower()
        for ln in body.splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    if len(lines) < MIN_ENTRIES:
        print(
            f"ERROR: only {len(lines)} entries fetched (< {MIN_ENTRIES}), refusing to overwrite",
            file=sys.stderr,
        )
        return 1
    DEST.parent.mkdir(parents=True, exist_ok=True)
    DEST.write_text("\n".join(sorted(set(lines))) + "\n", encoding="ascii", errors="ignore")
    print(f"Wrote {len(set(lines))} disposable domains to {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
