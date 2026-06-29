"""Post-deploy DB initialisation — run by the db_init GitHub Actions workflow.

Replaces the startup DB ops that were previously run inside the Vercel lifespan
(which pushed cold-start time over Hobby's 10s limit). Tables, lightweight
migrations, admin bootstrap, and team-owner backfill all happen here instead.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import bcrypt
from sqlmodel import Session, select

from app.db import backfill_team_owners, create_db_tables, engine
from app.models import User


def _bootstrap_admin() -> None:
    admin_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    superadmin_email = os.getenv("SUPERADMIN_EMAIL", "").strip().lower()

    with Session(engine) as db:
        if admin_email and admin_password:
            if not db.exec(select(User)).first():
                pw_hash = bcrypt.hashpw(
                    admin_password.encode(), bcrypt.gensalt(rounds=12)
                ).decode()
                db.add(User(
                    email=admin_email,
                    password_hash=pw_hash,
                    role="admin",
                    is_active=True,
                ))
                db.commit()
                print(f"[db_init] created admin: {admin_email}")

        if superadmin_email:
            user = db.exec(select(User).where(User.email == superadmin_email)).first()
            if user:
                if user.role != "superadmin":
                    user.role = "superadmin"
                    user.is_active = True
                    db.commit()
                    print(f"[db_init] promoted {superadmin_email} → superadmin")
            elif admin_password:
                pw_hash = bcrypt.hashpw(
                    admin_password.encode(), bcrypt.gensalt(rounds=12)
                ).decode()
                db.add(User(
                    email=superadmin_email,
                    password_hash=pw_hash,
                    role="superadmin",
                    is_active=True,
                ))
                db.commit()
                print(f"[db_init] created superadmin: {superadmin_email}")


if __name__ == "__main__":
    print("[db_init] creating/migrating tables …")
    create_db_tables()
    print("[db_init] bootstrapping admin …")
    _bootstrap_admin()
    print("[db_init] backfilling team owners …")
    backfill_team_owners()
    print("[db_init] done.")
