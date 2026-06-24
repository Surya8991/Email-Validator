"""
One-shot script to create all tables in PostgreSQL (Neon).
Run this ONCE after creating your Neon database.

Usage:
    DATABASE_URL=postgresql+psycopg2://... python scripts/init_db.py
    -- or --
    Add DATABASE_URL to .env, then:
    python scripts/init_db.py
"""

import os
import sys
from pathlib import Path

# Ensure app package is importable from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load .env if present
try:
    from dotenv import load_dotenv  # type: ignore[import]
    load_dotenv()
except ImportError:
    pass  # dotenv optional; DATABASE_URL can be set in environment directly

from sqlmodel import SQLModel

from app.config import settings
from app.db import engine

# Import models so SQLModel.metadata knows about them
import app.models  # noqa: F401


def main() -> None:
    url = settings.database_url or os.getenv("DATABASE_URL", "")
    if not url:
        print("ERROR: DATABASE_URL is not set.")
        print("  Set it in .env or export it before running this script.")
        print("  Example: DATABASE_URL=postgresql+psycopg2://user:pass@host/db")
        sys.exit(1)

    # Normalize for display (hide password)
    display_url = url
    if "@" in url:
        scheme_user, rest = url.split("@", 1)
        display_url = scheme_user.split(":")[0] + "://***@" + rest

    print(f"Connecting to: {display_url}")
    print("Creating tables...")

    SQLModel.metadata.create_all(engine)

    print()
    print("Done. Tables created (or already existed — idempotent):")
    for table in SQLModel.metadata.sorted_tables:
        print(f"  - {table.name}")
    print()
    print("Your Neon database is ready. Set DATABASE_URL in Vercel + GitHub Secrets.")


if __name__ == "__main__":
    main()
