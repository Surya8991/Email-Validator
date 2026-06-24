import hashlib
import secrets
from datetime import datetime, timedelta

from fastapi import Depends, Request
from sqlmodel import Session, select

from app.db import get_session
from app.models import User, UserSession

SESSION_COOKIE = "ev_session"
SESSION_TTL_DAYS = 7


class RequiresAuth(Exception):
    pass


class RequiresAdmin(Exception):
    pass


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_user_session(user: User, db: Session) -> str:
    old = db.exec(select(UserSession).where(UserSession.user_id == user.id)).all()
    for s in old:
        db.delete(s)

    token = secrets.token_urlsafe(32)
    db.add(UserSession(
        user_id=user.id,
        token_hash=_hash_token(token),
        expires_at=datetime.utcnow() + timedelta(days=SESSION_TTL_DAYS),
    ))
    db.commit()
    return token


def delete_user_session(token: str, db: Session) -> None:
    s = db.exec(select(UserSession).where(UserSession.token_hash == _hash_token(token))).first()
    if s:
        db.delete(s)
        db.commit()


def get_current_user(request: Request, db: Session = Depends(get_session)) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    s = db.exec(select(UserSession).where(UserSession.token_hash == _hash_token(token))).first()
    if not s:
        return None
    now = datetime.utcnow()
    if s.expires_at < now:
        db.delete(s)
        db.commit()
        return None
    s.expires_at = now + timedelta(days=SESSION_TTL_DAYS)
    db.commit()
    return db.get(User, s.user_id)


def require_auth(request: Request, db: Session = Depends(get_session)) -> User:
    user = get_current_user(request, db)
    if not user or not user.is_active:
        raise RequiresAuth()
    return user


def require_admin(user: User = Depends(require_auth)) -> User:
    if user.role not in ("admin", "superadmin"):
        raise RequiresAdmin()
    return user


def require_superadmin(user: User = Depends(require_auth)) -> User:
    if user.role != "superadmin":
        raise RequiresAdmin()
    return user
