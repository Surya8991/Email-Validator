"""One-off: flip every EmailResult + EmailCache row whose domain is in
the typo blocklist (see app/providers/local.py: _TYPO_DOMAINS) to
verdict='invalid'.

These addresses can never deliver — they're single-letter typos of
gmail.com / yahoo.com / hotmail.com / etc. with no working MX. Marking
them invalid means future bulk runs short-circuit them via cache and
the new in-LocalProvider check, never paying for a Bouncify call.

Usage:
    DATABASE_URL=postgres://... python scripts/flip_typo_domain_rows.py --dry-run
    DATABASE_URL=postgres://... python scripts/flip_typo_domain_rows.py
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import or_  # noqa: E402
from sqlalchemy import update as sa_update
from sqlmodel import Session, select  # noqa: E402

from app.db import engine  # noqa: E402
from app.models import EmailCache, EmailResult  # noqa: E402
from app.providers.local import _TYPO_DOMAINS  # noqa: E402


def _domain_match_clause(model):
    """Build `email ILIKE '%@<typo>'` for every domain in the blocklist.
    Combined via OR for one efficient WHERE clause."""
    return or_(*[model.email.ilike(f"%@{d}") for d in sorted(_TYPO_DOMAINS)])


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="Print the affected row counts without updating.")
    args = p.parse_args()

    print(f"Typo-domain blocklist size: {len(_TYPO_DOMAINS)}", flush=True)

    with Session(engine) as db:
        # EmailResult
        result_count = db.execute(
            select(EmailResult.email).where(
                _domain_match_clause(EmailResult),
                EmailResult.verdict != "invalid",
            )
        ).all()
        n_result = len(result_count)
        print(f"EmailResult rows to flip: {n_result}", flush=True)

        # EmailCache
        cache_count = db.execute(
            select(EmailCache.email).where(
                _domain_match_clause(EmailCache),
                EmailCache.verdict != "invalid",
            )
        ).all()
        n_cache = len(cache_count)
        print(f"EmailCache rows to flip: {n_cache}", flush=True)

        if args.dry_run:
            print("[dry-run] nothing updated.", flush=True)
            return 0

        if n_result == 0 and n_cache == 0:
            print("Nothing to do — every typo-domain row already invalid.", flush=True)
            return 0

        # Apply the flip.
        result_flipped = db.execute(
            sa_update(EmailResult)
            .where(
                _domain_match_clause(EmailResult),
                EmailResult.verdict != "invalid",
            )
            .values(verdict="invalid"),
        ).rowcount or 0

        cache_flipped = db.execute(
            sa_update(EmailCache)
            .where(
                _domain_match_clause(EmailCache),
                EmailCache.verdict != "invalid",
            )
            .values(verdict="invalid"),
        ).rowcount or 0

        db.commit()
        print(
            f"Flipped {result_flipped} EmailResult rows and "
            f"{cache_flipped} EmailCache rows to verdict='invalid'.",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
