import asyncio
import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from pathlib import Path

import bcrypt
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.auth import SESSION_COOKIE, SESSION_TTL_DAYS, create_user_session, delete_user_session
from app.config import settings
from app.db import get_session
from app.models import PasswordReset, SystemSetting, User, UserInvite
from app.services.email import (
    send_account_approved_email,
    send_password_reset_email,
    send_pending_approval_notice,
)

logger = logging.getLogger(__name__)
PASSWORD_RESET_TTL_MINUTES = 30

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


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
    user = db.exec(select(User).where(User.email == email.strip().lower())).first()
    if not user or not _verify_password(password, user.password_hash):
        return templates.TemplateResponse(request, "auth/login.html", {
            "error": "Invalid email or password."
        }, status_code=401)
    if not user.is_active:
        return templates.TemplateResponse(request, "auth/login.html", {
            "error": "Your account is pending admin approval."
        }, status_code=403)

    token = create_user_session(user, db)
    user.last_login = datetime.utcnow()
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
            base_url = str(request.base_url).rstrip("/")
            admin_url = f"{base_url}/admin/users?status=pending"
            await asyncio.gather(
                *(send_pending_approval_notice(a.email, new_email, admin_url) for a in admins),
                return_exceptions=True,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Admin pending-approval notification failed: %s", e)

    return templates.TemplateResponse(request, "auth/register.html", {"success": True})


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

        base_url = str(request.base_url).rstrip("/")
        reset_url = f"{base_url}/reset-password/{raw_token}"
        try:
            await send_password_reset_email(user.email, reset_url, PASSWORD_RESET_TTL_MINUTES)
        except Exception as e:  # noqa: BLE001
            logger.exception("Password-reset email failed: %s", e)

    return templates.TemplateResponse(request, "auth/forgot_password.html", {"sent": True})


def _resolve_reset(token: str, db: Session) -> PasswordReset | None:
    pr = db.exec(select(PasswordReset).where(PasswordReset.token_hash == _hash_token(token))).first()
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
