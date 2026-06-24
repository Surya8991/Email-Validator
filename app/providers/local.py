import asyncio
import smtplib

import dns.resolver
from email_validator import EmailNotValidError, validate_email

from app.config import settings
from app.schemas import ProviderResult

# Maintained list from disposable-email-domains package
try:
    import disposable_email_domains
    _DISPOSABLE: set[str] = set(disposable_email_domains.blocklist)
except Exception:
    _DISPOSABLE = set()

_ROLE_PREFIXES = {
    "admin", "info", "contact", "support", "help", "noreply", "no-reply",
    "sales", "marketing", "billing", "abuse", "postmaster", "webmaster",
    "hostmaster", "security", "privacy", "legal", "newsletter", "notifications",
    "hello", "enquiries", "enquiry", "careers", "jobs", "press", "media",
    "feedback", "team", "office", "accounts", "hr",
}

_FREE_PROVIDERS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com",
    "hotmail.co.uk", "outlook.com", "live.com", "msn.com", "aol.com",
    "icloud.com", "me.com", "mac.com", "protonmail.com", "proton.me",
    "tutanota.com", "zohomail.com", "yandex.com", "mail.ru",
}

# Cache catch-all domains to avoid repeated SMTP probes
_CATCH_ALL_CACHE: dict[str, bool] = {}


class LocalProvider:
    name = "local"

    async def verify(self, email: str) -> ProviderResult:
        # 1. Syntax check
        try:
            info = validate_email(email, check_deliverability=False)
            normalized = info.normalized
        except EmailNotValidError as e:
            return ProviderResult(
                status="invalid", sub_status="syntax_error", raw={"error": str(e)}
            )

        local_part, domain = normalized.split("@", 1)
        domain = domain.lower()

        is_disposable = domain in _DISPOSABLE
        is_role = local_part.lower() in _ROLE_PREFIXES
        is_free = domain in _FREE_PROVIDERS

        if is_disposable:
            return ProviderResult(
                status="risky", sub_status="disposable",
                is_disposable=True, is_role=is_role, is_free=is_free,
                raw={"domain": domain}
            )

        # 2. MX lookup
        mx_found = await _check_mx(domain)
        if not mx_found:
            return ProviderResult(
                status="invalid", sub_status="no_mx",
                is_role=is_role, is_free=is_free, mx_found=False,
                raw={"domain": domain}
            )

        # 3. SMTP probe (optional)
        if settings.enable_smtp_probe:
            smtp_status = await asyncio.to_thread(_smtp_probe, normalized, domain)
            if smtp_status == "valid":
                return ProviderResult(
                    status="valid", sub_status="smtp_confirmed",
                    is_role=is_role, is_free=is_free, mx_found=True,
                    raw={"smtp": "250"}
                )
            elif smtp_status == "invalid":
                return ProviderResult(
                    status="invalid", sub_status="smtp_rejected",
                    is_role=is_role, is_free=is_free, mx_found=True,
                    raw={"smtp": "550"}
                )

        sub = "role_based" if is_role else ("free_provider" if is_free else "")
        return ProviderResult(
            status="valid", sub_status=sub,
            is_role=is_role, is_free=is_free, mx_found=True,
            raw={"domain": domain}
        )

    async def verify_bulk(self, emails: list[str]) -> list[ProviderResult]:
        results = await asyncio.gather(*[self.verify(e) for e in emails])
        return list(results)


async def _check_mx(domain: str) -> bool:
    try:
        await asyncio.to_thread(dns.resolver.resolve, domain, "MX")
        return True
    except Exception:
        # Fall back to A record — some domains skip MX
        try:
            await asyncio.to_thread(dns.resolver.resolve, domain, "A")
            return True
        except Exception:
            return False


def _smtp_probe(email: str, domain: str) -> str:
    if _CATCH_ALL_CACHE.get(domain):
        return "unknown"
    try:
        answers = dns.resolver.resolve(domain, "MX")
        mx_host = str(sorted(answers, key=lambda r: r.preference)[0].exchange).rstrip(".")
        with smtplib.SMTP(timeout=10) as smtp:
            smtp.connect(mx_host, 25)
            smtp.helo("validator.local")
            smtp.mail(settings.smtp_probe_from)
            code, _ = smtp.rcpt(email)
            if code == 250:
                return "valid"
            elif code == 550:
                return "invalid"
            return "unknown"
    except Exception:
        return "unknown"
