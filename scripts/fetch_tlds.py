"""Refresh the IANA TLD snapshot used by local_rules.is_known_tld().

Usage: python scripts/fetch_tlds.py
Idempotent — overwrites app/data/tlds.txt with the current IANA list.
"""

import sys
import urllib.request
from pathlib import Path

IANA_URL = "https://data.iana.org/TLD/tlds-alpha-by-domain.txt"
DEST = Path(__file__).resolve().parent.parent / "app" / "data" / "tlds.txt"


def main() -> int:
    with urllib.request.urlopen(IANA_URL, timeout=30) as resp:
        body = resp.read().decode("ascii")
    tld_count = sum(1 for ln in body.splitlines() if ln and not ln.startswith("#"))
    if tld_count < 1000:  # sanity guard against a truncated download
        print(f"ERROR: only {tld_count} TLDs fetched, refusing to overwrite", file=sys.stderr)
        return 1
    DEST.parent.mkdir(parents=True, exist_ok=True)
    DEST.write_text(body, encoding="ascii")
    print(f"Wrote {tld_count} TLDs to {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
