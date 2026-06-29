import asyncio
import html
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from app.config import settings


def _e(value: str) -> str:
    """Escape user-supplied text before interpolating into HTML email bodies."""
    return html.escape(value or "", quote=True)

logger = logging.getLogger(__name__)


class EmailNotConfigured(RuntimeError):
    pass


def _build_message(to_email: str, subject: str, text: str, html: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((settings.smtp_from_name or "", settings.smtp_from or settings.smtp_user))
    msg["To"] = to_email
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def _send_sync(msg: EmailMessage) -> None:
    host = settings.smtp_host
    port = settings.smtp_port
    user = settings.smtp_user
    password = settings.smtp_password
    timeout = settings.smtp_timeout

    if not host or not port:
        raise EmailNotConfigured("SMTP_HOST / SMTP_PORT not set")

    context = ssl.create_default_context()

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=timeout, context=context) as s:
            if user and password:
                s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as s:
            s.ehlo()
            if settings.smtp_use_tls:
                s.starttls(context=context)
                s.ehlo()
            if user and password:
                s.login(user, password)
            s.send_message(msg)


async def send_email(to_email: str, subject: str, text: str, html: str) -> None:
    msg = _build_message(to_email, subject, text, html)
    await asyncio.to_thread(_send_sync, msg)


def _invite_templates(invite_url: str, role: str, inviter_email: str) -> tuple[str, str, str]:
    subject = "You're invited to Email Validator"
    role_line = "as an admin" if role == "admin" else ""
    text = (
        f"{inviter_email} has invited you to Email Validator {role_line}.\n\n"
        f"Activate your account: {invite_url}\n\n"
        "This link expires in 7 days and can only be used once.\n"
    )
    inviter_e = _e(inviter_email)
    url_e = _e(invite_url)
    role_suffix = " as an admin" if role == "admin" else ""
    html_body = f"""\
<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#f6f7fb;padding:24px;color:#111">
  <div style="max-width:520px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:32px">
    <div style="font-weight:700;font-size:18px;margin-bottom:8px">
      <span style="display:inline-block;width:28px;height:28px;background:#4f46e5;color:#fff;border-radius:8px;text-align:center;line-height:28px;margin-right:6px">✉</span>
      Email<span style="color:#4f46e5">Validator</span>
    </div>
    <h1 style="font-size:20px;margin:16px 0 8px">You're invited{role_suffix}</h1>
    <p style="color:#4b5563;font-size:14px;line-height:1.5">
      <strong>{inviter_e}</strong> invited you to Email Validator. Click the button below to set a password and activate your account.
    </p>
    <p style="margin:24px 0">
      <a href="{url_e}"
         style="background:#4f46e5;color:#fff;text-decoration:none;font-weight:600;font-size:14px;padding:12px 20px;border-radius:10px;display:inline-block">
        Activate account →
      </a>
    </p>
    <p style="color:#6b7280;font-size:12px;line-height:1.5">
      Or copy this link into your browser:<br>
      <span style="word-break:break-all">{url_e}</span>
    </p>
    <p style="color:#9ca3af;font-size:12px;margin-top:24px">This link expires in 7 days and can only be used once.</p>
  </div>
</body></html>"""
    return subject, text, html_body


async def send_invite_email(to_email: str, invite_url: str, role: str, inviter_email: str) -> None:
    subject, text, html = _invite_templates(invite_url, role, inviter_email)
    await send_email(to_email, subject, text, html)


def _shell(title: str, body_html: str) -> str:
    return f"""\
<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#f6f7fb;padding:24px;color:#111">
  <div style="max-width:520px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:32px">
    <div style="font-weight:700;font-size:18px;margin-bottom:8px">
      <span style="display:inline-block;width:28px;height:28px;background:#4f46e5;color:#fff;border-radius:8px;text-align:center;line-height:28px;margin-right:6px">✉</span>
      Email<span style="color:#4f46e5">Validator</span>
    </div>
    <h1 style="font-size:20px;margin:16px 0 8px">{title}</h1>
    {body_html}
  </div>
</body></html>"""


async def send_pending_approval_notice(to_admin_email: str, new_user_email: str, admin_url: str) -> None:
    subject = f"New user pending approval: {new_user_email}"
    text = (
        f"A new user has registered and is waiting for approval:\n\n"
        f"  {new_user_email}\n\n"
        f"Review at: {admin_url}\n"
    )
    body = f"""
    <p style="color:#4b5563;font-size:14px;line-height:1.5">
      A new user has registered and is waiting for your approval:
    </p>
    <p style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:12px;font-family:ui-monospace,Menlo,monospace;font-size:13px">
      {_e(new_user_email)}
    </p>
    <p style="margin:24px 0">
      <a href="{_e(admin_url)}" style="background:#4f46e5;color:#fff;text-decoration:none;font-weight:600;font-size:14px;padding:12px 20px;border-radius:10px;display:inline-block">
        Review user →
      </a>
    </p>
    """
    await send_email(to_admin_email, subject, text, _shell("New user awaiting approval", body))


async def send_account_approved_email(to_email: str, login_url: str) -> None:
    subject = "Your Email Validator account is approved"
    text = (
        "Good news — an admin has approved your Email Validator account.\n\n"
        f"Sign in: {login_url}\n"
    )
    body = f"""
    <p style="color:#4b5563;font-size:14px;line-height:1.5">
      An admin has approved your account. You can now sign in and start validating.
    </p>
    <p style="margin:24px 0">
      <a href="{_e(login_url)}" style="background:#4f46e5;color:#fff;text-decoration:none;font-weight:600;font-size:14px;padding:12px 20px;border-radius:10px;display:inline-block">
        Sign in →
      </a>
    </p>
    """
    await send_email(to_email, subject, text, _shell("Account approved", body))


async def send_team_join_decided_email(
    to_email: str, team_name: str, decision: str, app_url: str
) -> None:
    approved = decision == "approved"
    subject = f"Your request to join {team_name} was {'approved' if approved else 'declined'}"
    text = (
        f"Your request to join the team \"{team_name}\" was {decision}.\n\n"
        + (f"Open the app: {app_url}\n" if approved else "")
    )
    team_e = _e(team_name)
    if approved:
        body = f"""
        <p style="color:#4b5563;font-size:14px;line-height:1.5">
          Good news — your request to join <strong>{team_e}</strong> was approved.
          You're now an active member.
        </p>
        <p style="margin:24px 0">
          <a href="{_e(app_url)}" style="background:#4f46e5;color:#fff;text-decoration:none;font-weight:600;font-size:14px;padding:12px 20px;border-radius:10px;display:inline-block">
            Open the app →
          </a>
        </p>
        """
        title = f"You're in: {team_e}"
    else:
        body = f"""
        <p style="color:#4b5563;font-size:14px;line-height:1.5">
          Your request to join <strong>{team_e}</strong> was declined.
          If you think this was a mistake, reach out to a team owner or admin.
        </p>
        """
        title = "Join request declined"
    await send_email(to_email, subject, text, _shell(title, body))


async def send_password_reset_email(to_email: str, reset_url: str, ttl_minutes: int) -> None:
    subject = "Reset your Email Validator password"
    text = (
        "We received a request to reset your Email Validator password.\n\n"
        f"Reset link (expires in {ttl_minutes} minutes): {reset_url}\n\n"
        "If you didn't request this, you can ignore this email.\n"
    )
    url_e = _e(reset_url)
    body = f"""
    <p style="color:#4b5563;font-size:14px;line-height:1.5">
      We received a request to reset your password. Click the button below to choose a new one.
      This link expires in <strong>{ttl_minutes} minutes</strong> and can only be used once.
    </p>
    <p style="margin:24px 0">
      <a href="{url_e}" style="background:#4f46e5;color:#fff;text-decoration:none;font-weight:600;font-size:14px;padding:12px 20px;border-radius:10px;display:inline-block">
        Reset password →
      </a>
    </p>
    <p style="color:#6b7280;font-size:12px;line-height:1.5">
      Or copy this link into your browser:<br>
      <span style="word-break:break-all">{url_e}</span>
    </p>
    <p style="color:#9ca3af;font-size:12px;margin-top:24px">
      If you didn't request a password reset, ignore this email — your password won't change.
    </p>
    """
    await send_email(to_email, subject, text, _shell("Password reset", body))
