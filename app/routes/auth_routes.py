import hashlib
from datetime import datetime
from pathlib import Path

import bcrypt
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.auth import SESSION_COOKIE, SESSION_TTL_DAYS, create_user_session, delete_user_session
from app.config import settings
from app.db import get_session
from app.models import SystemSetting, User, UserInvite

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

    db.add(User(
        email=email.strip().lower(),
        password_hash=_hash_password(password),
        role="user",
        is_active=False,
    ))
    db.commit()
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
