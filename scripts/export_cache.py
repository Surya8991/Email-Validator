"""Dump the EmailCache table to a CSV file on disk.

Used by .github/workflows/export_cache.yml when the cache is too big
for the Vercel-side /api/cache/export endpoint (10s function timeout).
The workflow runs this script then uploads the file as a build
artifact the user can download from the Actions UI.

Column projection drops `provider_data` (the fat JSON blob) — same
6-column shape as the in-app export.

Usage:
    DATABASE_URL=postgres://... python scripts/export_cache.py
    DATABASE_URL=postgres://... python scripts/export_cache.py --out /tmp/cache.csv
    DATABASE_URL=postgres://... python scripts/export_cache.py --verdict valid
    DATABASE_URL=postgres://... python scripts/export_cache.py --q '@gmail.com'
"""
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlmodel import Session, select  # noqa: E402

from app.db import engine  # noqa: E402
from app.models import EmailCache  # noqa: E402

_VALID_VERDICTS = {"valid", "invalid", "risky"}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default="cache-export.csv",
                   help="Output CSV path (default: cache-export.csv)")
    p.add_argument("--verdict", default="",
                   help="Filter to one verdict: valid | invalid | risky")
    p.add_argument("--q", default="",
                   help="Substring match on email column")
    args = p.parse_args()

    v = args.verdict.strip().lower()
    verdict_q = v if v in _VALID_VERDICTS else ""

    stmt = (
        select(
            EmailCache.email, EmailCache.verdict, EmailCache.providers_used,
            EmailCache.strategy, EmailCache.validated_at, EmailCache.expires_at,
        )
        .order_by(EmailCache.validated_at.desc())  # type: ignore[arg-type]
    )
    if args.q:
        stmt = stmt.where(EmailCache.email.contains(args.q))
    if verdict_q:
        stmt = stmt.where(EmailCache.verdict == verdict_q)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    n = 0
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "email", "verdict", "providers_used", "strategy",
            "validated_at", "expires_at",
        ])
        # Chunked fetch keeps memory bounded on huge caches.
        with Session(engine) as session:
            for email, vd, providers_used, strategy, validated_at, expires_at in (
                session.execute(stmt).all()
            ):
                writer.writerow([
                    email or "",
                    vd or "",
                    providers_used or "",
                    strategy or "",
                    validated_at.isoformat() if validated_at else "",
                    expires_at.isoformat() if expires_at else "",
                ])
                n += 1

    size_bytes = out_path.stat().st_size
    print(
        f"Wrote {n} rows to {out_path} ({size_bytes:,} bytes / "
        f"~{size_bytes // 1024} KB)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
