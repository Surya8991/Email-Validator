import asyncio
import hashlib
import logging
import secrets
from datetime import datetime, timedelta

import bcrypt
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from app.auth import (
    SESSION_COOKIE,
    SESSION_TTL_DAYS,
    create_user_session,
    delete_user_session,
    require_auth,
)
from app.config import settings
from app.db import get_session
from app.models import PasswordReset, SystemSetting, User, UserInvite, UserSession
from app.security.rate_limit import rate_limit
from app.services.email import (
    send_password_reset_email,
    send_pending_approval_notice,
)
from app.templating import templates

logger = logging.getLogger(__name__)
PASSWORD_RESET_TTL_MINUTES = 30

# Login rate limit: N consecutive failures locks the account for LOCKOUT_MINUTES.
LOGIN_MAX_FAILS = 5
LOGIN_LOCKOUT_MINUTES = 15

router = APIRouter()


def _public_base_url(request: Request) -> str:
    """Origin used for outbound email links. Prefer the env-pinned BASE_URL so
    a spoofed Host header in a misconfigured proxy can't redirect victims to
    an attacker's domain. Falls back to request.base_url for local dev only.
    """
    if settings.base_url:
        return settings.base_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def _revoke_all_sessions(user_id: int, db: Session) -> None:
    sessions = db.exec(select(UserSession).where(UserSession.user_id == user_id)).all()
    for s in sessions:
        db.delete(s)


def _verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "auth/login.html", {})


@router.post("/login")
async def login_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_session),
):
    # Per-IP burst limit applies whether or not the email exists, so a brute-force
    # scan against unknown emails (which previously bypassed the account lockout)
    # now gets throttled too.
    rate_limit(request, "login", max_hits=10, window_seconds=60)

    user = db.exec(select(User).where(User.email == email.strip().lower())).first()
    now = datetime.utcnow()

    if user and user.locked_until and user.locked_until > now:
        mins_left = max(1, int((user.locked_until - now).total_seconds() // 60))
        return templates.TemplateResponse(request, "auth/login.html", {
            "error": f"Too many failed attempts. Try again in {mins_left} minute(s).",
        }, status_code=429)

    if not user or not _verify_password(password, user.password_hash):
        if user:
            user.failed_login_count = (user.failed_login_count or 0) + 1
            if user.failed_login_count >= LOGIN_MAX_FAILS:
                user.locked_until = now + timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
                user.failed_login_count = 0
            db.commit()
        return templates.TemplateResponse(request, "auth/login.html", {
            "error": "Invalid email or password."
        }, status_code=401)
    if not user.is_active:
        return templates.TemplateResponse(request, "auth/login.html", {
            "error": "Your account is pending admin approval."
        }, status_code=403)

    # Successful login — clear lockout counter.
    user.failed_login_count = 0
    user.locked_until = None
    token = create_user_session(user, db)
    user.last_login = now
    db.commit()

    resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL_DAYS * 86400,
        secure=settings.production,
    )
    return resp


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse(request, "auth/register.html", {})


@router.post("/register")
async def register_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_session),
):
    rate_limit(request, "register", max_hits=5, window_seconds=300)
    reg_open = db.get(SystemSetting, "registration_open")
    if reg_open and reg_open.value == "0":
        return templates.TemplateResponse(request, "auth/register.html", {
            "error": "Registration is currently closed. Contact an admin for an invite."
        }, status_code=403)
    if password != confirm_password:
        return templates.TemplateResponse(request, "auth/register.html", {
            "error": "Passwords do not match."
        }, status_code=400)
    if len(password) < 8:
        return templates.TemplateResponse(request, "auth/register.html", {
            "error": "Password must be at least 8 characters."
        }, status_code=400)

    existing = db.exec(select(User).where(User.email == email.strip().lower())).first()
    if existing:
        return templates.TemplateResponse(request, "auth/register.html", {
            "error": "An account with this email already exists."
        }, status_code=400)

    new_email = email.strip().lower()
    db.add(User(
        email=new_email,
        password_hash=_hash_password(password),
        role="user",
        is_active=False,
    ))
    db.commit()

    # Notify all admins/superadmins so they can approve the user.
    if settings.smtp_host:
        try:
            admins = db.exec(
                select(User).where(User.role.in_(["admin", "superadmin"]), User.is_active == True)  # noqa: E712
            ).all()
            admin_url = f"{_public_base_url(request)}/admin/users?status=pending"
            await asyncio.gather(
                *(send_pending_approval_notice(a.email, new_email, admin_url) for a in admins),
                return_exceptions=True,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Admin pending-approval notification failed: %s", e)

    return templates.TemplateResponse(request, "auth/register.html", {"success": True})


@router.get("/profile", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    current_user: User = Depends(require_auth),
    db: Session = Depends(get_session),
):
    sessions = db.exec(select(UserSession).where(UserSession.user_id == current_user.id)).all()
    return templates.TemplateResponse(request, "auth/profile.html", {
        "current_user": current_user,
        "session_count": len(sessions),
        "saved": request.query_params.get("saved"),
        "error": request.query_params.get("err"),
    })


@router.post("/profile/email")
async def profile_change_email(
    request: Request,
    new_email: str = Form(...),
    current_password: str = Form(...),
    current_user: User = Depends(require_auth),
    db: Session = Depends(get_session),
):
    new_email = new_email.strip().lower()
    if not _verify_password(current_password, current_user.password_hash):
        return RedirectResponse(url="/profile?err=bad_password", status_code=302)
    if "@" not in new_email or "." not in new_email:
        return RedirectResponse(url="/profile?err=invalid_email", status_code=302)
    if new_email == current_user.email:
        return RedirectResponse(url="/profile?saved=email", status_code=302)
    clash = db.exec(select(User).where(User.email == new_email)).first()
    if clash:
        return RedirectResponse(url="/profile?err=email_taken", status_code=302)
    from app.models import AuditLog
    db.add(AuditLog(
        action="profile.email.change",
        actor_id=current_user.id,
        actor_email=current_user.email,
        target_type="user",
        target_id=str(current_user.id),
        details=f"new_email={new_email}",
    ))
    current_user.email = new_email
    db.commit()
    return RedirectResponse(url="/profile?saved=email", status_code=302)


@router.post("/profile/password")
async def profile_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    current_user: User = Depends(require_auth),
    db: Session = Depends(get_session),
):
    if not _verify_password(current_password, current_user.password_hash):
        return RedirectResponse(url="/profile?err=bad_password", status_code=302)
    if new_password != confirm_password:
        return RedirectResponse(url="/profile?err=mismatch", status_code=302)
    if len(new_password) < 8:
        return RedirectResponse(url="/profile?err=too_short", status_code=302)
    from app.models import AuditLog
    db.add(AuditLog(
        action="profile.password.change",
        actor_id=current_user.id,
        actor_email=current_user.email,
        target_type="user",
        target_id=str(current_user.id),
    ))
    current_user.password_hash = _hash_password(new_password)
    # Phished sessions must NOT survive a password change.
    _revoke_all_sessions(current_user.id, db)
    new_token = create_user_session(current_user, db)
    db.commit()
    resp = RedirectResponse(url="/profile?saved=password", status_code=302)
    resp.set_cookie(
        key=SESSION_COOKIE,
        value=new_token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL_DAYS * 86400,
        secure=settings.production,
    )
    return resp


@router.post("/profile/sessions/revoke-all")
async def profile_revoke_all_sessions(
    request: Request,
    current_user: User = Depends(require_auth),
    db: Session = Depends(get_session),
):
    # Delete every session except the one making this request, so the user stays logged in here.
    current_token = request.cookies.get(SESSION_COOKIE)
    current_hash = hashlib.sha256(current_token.encode()).hexdigest() if current_token else None
    sessions = db.exec(select(UserSession).where(UserSession.user_id == current_user.id)).all()
    for s in sessions:
        if s.token_hash != current_hash:
            db.delete(s)
    db.commit()
    return RedirectResponse(url="/profile?saved=sessions", status_code=302)


@router.post("/logout")
async def logout(request: Request, db: Session = Depends(get_session)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        delete_user_session(token, db)
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


def _resolve_invite(token: str, db: Session) -> UserInvite | None:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    invite = db.exec(select(UserInvite).where(UserInvite.token_hash == token_hash)).first()
    if not invite or invite.used_at or invite.expires_at < datetime.utcnow():
        return None
    return invite


@router.get("/invite/{token}", response_class=HTMLResponse)
async def invite_page(token: str, request: Request, db: Session = Depends(get_session)):
    invite = _resolve_invite(token, db)
    if not invite:
        return templates.TemplateResponse(request, "auth/invite.html", {
            "invalid": True, "token": token,
        })
    return templates.TemplateResponse(request, "auth/invite.html", {
        "invite": invite, "token": token,
    })


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse(request, "auth/forgot_password.html", {})


@router.post("/forgot-password")
async def forgot_password_post(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_session),
):
    rate_limit(request, "forgot-password", max_hits=5, window_seconds=300)
    email_norm = email.strip().lower()
    user = db.exec(select(User).where(User.email == email_norm)).first()

    # Always show the same success page — don't leak which emails are registered.
    if user and user.is_active and settings.smtp_host:
        # Invalidate any prior unused reset for this user.
        old = db.exec(
            select(PasswordReset).where(
                PasswordReset.user_id == user.id,
                PasswordReset.used_at == None,  # noqa: E711
            )
        ).all()
        for r in old:
            db.delete(r)

        raw_token = secrets.token_urlsafe(32)
        db.add(PasswordReset(
            user_id=user.id,
            token_hash=_hash_token(raw_token),
            expires_at=datetime.utcnow() + timedelta(minutes=PASSWORD_RESET_TTL_MINUTES),
        ))
        db.commit()

        reset_url = f"{_public_base_url(request)}/reset-password/{raw_token}"
        try:
            await send_password_reset_email(user.email, reset_url, PASSWORD_RESET_TTL_MINUTES)
        except Exception as e:  # noqa: BLE001
            logger.exception("Password-reset email failed: %s", e)

    return templates.TemplateResponse(request, "auth/forgot_password.html", {"sent": True})


def _resolve_reset(token: str, db: Session) -> PasswordReset | None:
    pr = db.exec(
        select(PasswordReset).where(PasswordReset.token_hash == _hash_token(token))
    ).first()
    if not pr or pr.used_at or pr.expires_at < datetime.utcnow():
        return None
    return pr


@router.get("/reset-password/{token}", response_class=HTMLResponse)
async def reset_password_page(token: str, request: Request, db: Session = Depends(get_session)):
    pr = _resolve_reset(token, db)
    if not pr:
        return templates.TemplateResponse(request, "auth/reset_password.html", {
            "invalid": True, "token": token,
        })
    return templates.TemplateResponse(request, "auth/reset_password.html", {"token": token})


@router.post("/reset-password/{token}")
async def reset_password_post(
    token: str,
    request: Request,
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_session),
):
    pr = _resolve_reset(token, db)
    if not pr:
        return templates.TemplateResponse(request, "auth/reset_password.html", {
            "invalid": True, "token": token,
        })
    if password != confirm_password:
        return templates.TemplateResponse(request, "auth/reset_password.html", {
            "token": token, "error": "Passwords do not match.",
        }, status_code=400)
    if len(password) < 8:
        return templates.TemplateResponse(request, "auth/reset_password.html", {
            "token": token, "error": "Password must be at least 8 characters.",
        }, status_code=400)

    user = db.get(User, pr.user_id)
    if not user:
        return templates.TemplateResponse(request, "auth/reset_password.html", {
            "invalid": True, "token": token,
        })

    user.password_hash = _hash_password(password)
    pr.used_at = datetime.utcnow()
    # Any session opened before the reset is now untrusted — drop them all.
    _revoke_all_sessions(user.id, db)
    db.commit()
    return templates.TemplateResponse(request, "auth/reset_password.html", {"success": True})


@router.post("/invite/{token}")
async def invite_accept(
    token: str,
    request: Request,
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_session),
):
    invite = _resolve_invite(token, db)
    if not invite:
        return templates.TemplateResponse(request, "auth/invite.html", {
            "invalid": True, "token": token,
        })

    if password != confirm_password:
        return templates.TemplateResponse(request, "auth/invite.html", {
            "invite": invite, "token": token,
            "error": "Passwords do not match.",
        }, status_code=400)
    if len(password) < 8:
        return templates.TemplateResponse(request, "auth/invite.html", {
            "invite": invite, "token": token,
            "error": "Password must be at least 8 characters.",
        }, status_code=400)

    existing = db.exec(select(User).where(User.email == invite.email)).first()
    if existing:
        return templates.TemplateResponse(request, "auth/invite.html", {
            "invalid": True, "token": token,
            "error": "An account with this email already exists.",
        })

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    user = User(
        email=invite.email,
        password_hash=pw_hash,
        role=invite.role,
        is_active=True,
    )
    db.add(user)
    invite.used_at = datetime.utcnow()
    db.commit()
    db.refresh(user)

    session_token = create_user_session(user, db)
    resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie(
        key=SESSION_COOKIE,
        value=session_token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL_DAYS * 86400,
        secure=settings.production,
    )
    return resp
