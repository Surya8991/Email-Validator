import asyncio
import ipaddress
import secrets
from collections import OrderedDict
from pathlib import Path
from typing import Any

import aiosmtplib
import dns.asyncresolver
import dns.exception
import dns.resolver
from email_validator import EmailNotValidError, validate_email

from app.config import settings
from app.providers.local_rules import (
    NOREPLY_PREFIXES,
    canonicalize,
    compute_score,
    has_confusables,
    has_mixed_script,
    has_suspicious_pattern,
    is_known_tld,
    is_reserved_domain,
    registrable_domain,
    strip_invisible,
    suggest_domain,
)
from app.schemas import ProviderResult

# Disposable domains — prefer the fresh snapshot from scripts/fetch_disposable.py
# (upstream `disposable/disposable-email-domains`, ~110k entries) and fall
# back to the pypi `disposable-email-domains` package (~4k) if the snapshot
# is missing. Both are lowercase, so O(1) `in` checks against a lowercase
# domain are safe.
_DISPOSABLE_FILE = Path(__file__).resolve().parent.parent / "data" / "disposable.txt"


def _load_disposable() -> set[str]:
    if _DISPOSABLE_FILE.exists():
        try:
            return {
                ln.strip().lower()
                for ln in _DISPOSABLE_FILE.read_text(encoding="ascii").splitlines()
                if ln.strip() and not ln.startswith("#")
            }
        except Exception:
            pass
    try:
        import disposable_email_domains
        return set(disposable_email_domains.blocklist)
    except Exception:
        return set()


_DISPOSABLE: set[str] = _load_disposable()

_ROLE_PREFIXES = {
    # Original core set
    "admin", "info", "contact", "support", "help", "noreply", "no-reply",
    "sales", "marketing", "billing", "abuse", "postmaster", "webmaster",
    "hostmaster", "security", "privacy", "legal", "newsletter", "notifications",
    "hello", "enquiries", "enquiry", "careers", "jobs", "press", "media",
    "feedback", "team", "office", "accounts", "hr",
    # D4 expansion — Kickbox / ZeroBounce industry baseline
    "all", "everyone", "staff", "it", "general", "notify", "alert", "alerts",
    "complaints", "compliance", "customer", "customerservice", "customer-service",
    "customercare", "customer-care", "contactus", "contact-us", "contactme",
    "contact-me", "service", "ceo", "cfo", "cto", "coo", "mail", "mailer",
    "majordomo", "root", "nobody", "owner", "tech", "dev", "developer",
    "developers", "engineering", "ops", "admins", "mods", "moderators",
    "moderator", "register", "registration", "subscribe", "unsubscribe",
    "hi", "howdy", "mailer-daemon",
}

_FREE_PROVIDERS = {
    # Google / Yahoo / Microsoft family
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.co.uk", "yahoo.co.in", "yahoo.fr", "yahoo.de",
    "yahoo.co.jp", "yahoo.com.br", "ymail.com", "rocketmail.com",
    "hotmail.com", "hotmail.co.uk", "hotmail.fr", "hotmail.de",
    "outlook.com", "outlook.co.uk", "outlook.jp",
    "live.com", "live.co.uk", "msn.com",
    # AOL / Apple
    "aol.com", "aim.com", "icloud.com", "me.com", "mac.com",
    # Privacy-focused
    "protonmail.com", "proton.me", "pm.me", "tutanota.com", "tuta.io",
    "zohomail.com", "zoho.com",
    # Russian / CIS
    "yandex.com", "yandex.ru", "mail.ru",
    # Generic western
    "mail.com", "email.com",
    # DACH
    "gmx.com", "gmx.de", "gmx.net", "gmx.at", "gmx.ch", "gmx.us",
    "web.de", "t-online.de", "freenet.de", "arcor.de",
    # Italy
    "libero.it", "virgilio.it", "tiscali.it", "alice.it",
    # France
    "orange.fr", "wanadoo.fr", "laposte.net", "sfr.fr", "free.fr",
    # Benelux / CH
    "bluewin.ch", "telenet.be", "skynet.be", "ziggo.nl", "kpnmail.nl",
    "hetnet.nl", "home.nl", "xs4all.nl", "planet.nl",
    # AU
    "bigpond.com", "bigpond.net.au", "optusnet.com.au", "iinet.net.au",
    "telstra.com", "telstra.com.au",
    # Korea
    "naver.com", "daum.net", "hanmail.net", "kakao.com",
    # China
    "qq.com", "163.com", "126.com", "sina.com", "sohu.com", "foxmail.com",
    "sogou.com", "aliyun.com",
    # India
    "rediffmail.com", "indiatimes.com", "sify.com", "in.com",
    # Brazil
    "bol.com.br", "uol.com.br", "ig.com.br", "terra.com.br", "r7.com",
    # Israel
    "walla.co.il",
}

# Common typo domains for the major free-mail providers — single/
# transposed-letter mistakes that will never resolve to a working MX.
# Short-circuit these in `verify()` BEFORE the DNS / SMTP / external-API
# path so we don't burn Bouncify credits on them.
#
# Curated for high precision (zero false positives). NOT included:
#   - ymail.com (legitimate Yahoo Mail alias)
#   - googlemail.com (legitimate Google domain)
#   - mail.com, email.com (legitimate free providers)
#
# Add new entries here as you spot them in production. The runtime
# check is O(1) set lookup, so the list can grow freely.
_TYPO_DOMAINS = {
    # gmail.com typos
    "gmai.com", "gmial.com", "gmaill.com", "gmal.com", "gnail.com",
    "gmail.con", "gmail.co", "gmail.cm", "gmail.coom", "gmail.om",
    "gmsil.com", "gmali.com", "gemail.com", "gmaal.com", "gmaul.com",
    # yahoo.com typos
    "yaho.com", "yhaoo.com", "yhoo.com", "yahooo.com", "yahoo.con",
    "yahoo.cm", "gahoo.com", "gahooo.com", "yahho.com", "yaoo.com",
    "yahoo.om", "yahoocom", "yhoo.co",
    # hotmail.com typos
    "hotnail.com", "hotmial.com", "hotmaill.com", "hotamil.com",
    "hotmail.con", "hotmail.cm", "hotmali.com", "hotmail.om",
    "hotmal.com", "hotmail.co",
    # outlook.com typos
    "outlok.com", "outloook.com", "outloo.com", "outllok.com",
    "outlook.con", "outlok.co", "outloook.con", "outlook.om",
    "outlock.com", "outloook.co",
    # icloud.com typos
    "iclod.com", "icoud.com", "icloud.con", "icould.com",
    "icloud.cm", "icloud.om", "icoud.co", "iclooud.com",
    # aol.com typos
    "aol.con", "aoll.com", "aol.cm", "aol.om",
    # live.com / msn.com / proton typos
    "live.con", "live.cm", "msn.con",
    "proton.con", "protonmail.con", "protomail.com",
}

# Per-domain caches — bounded LRUs (D10) so long-running processes
# don't leak. Vercel serverless recycles anyway; the cap only matters
# on self-hosted uvicorn.
_MAX_CACHE = 10_000
_CATCH_ALL_CACHE: "OrderedDict[str, bool]" = OrderedDict()
_SPF_DMARC_CACHE: "OrderedDict[str, dict[str, bool]]" = OrderedDict()
_AUTH_CACHE: "OrderedDict[str, dict[str, bool]]" = OrderedDict()  # MTA-STS etc


def _cache_get(cache: "OrderedDict[str, Any]", key: str) -> Any:
    if key in cache:
        cache.move_to_end(key)
        return cache[key]
    return None


def _cache_set(cache: "OrderedDict[str, Any]", key: str, value: Any) -> None:
    if key in cache:
        cache.move_to_end(key)
    cache[key] = value
    while len(cache) > _MAX_CACHE:
        cache.popitem(last=False)


class LocalProvider:
    name = "local"

    async def verify(self, email: str) -> ProviderResult:
        codes: list[str] = []
        email, had_invisible = strip_invisible(email.strip())
        if had_invisible:
            codes.append("INVISIBLE_CHARS")

        # 1. Syntax check
        try:
            info = validate_email(email, check_deliverability=False)
            normalized = info.normalized
        except EmailNotValidError as e:
            return ProviderResult(
                status="invalid", sub_status="syntax_error",
                raw={"error": str(e)}, score=0,
                reason_codes=codes + ["SYNTAX_ERROR"],
            )

        local_part, domain = normalized.split("@", 1)
        domain = domain.lower()
        lp = local_part.lower()

        is_disposable = domain in _DISPOSABLE
        is_role = lp in _ROLE_PREFIXES
        is_noreply = lp in NOREPLY_PREFIXES
        is_free = domain in _FREE_PROVIDERS
        canonical = canonicalize(local_part, domain)
        suggestion: str | None = None

        if is_role:
            codes.append("ROLE_ACCOUNT")
        if is_noreply:
            codes.append("NO_REPLY")
        if is_free:
            codes.append("FREE_PROVIDER")
        if has_suspicious_pattern(local_part):
            codes.append("SUSPICIOUS_PATTERN")

        def _result(
            status: str, sub_status: str, *,
            mx_found: bool = False, smtp_confirmed: bool = False,
            raw: dict | None = None,
        ) -> ProviderResult:
            score = 0 if status == "invalid" else compute_score(
                codes, is_free=is_free, mx_found=mx_found,
                smtp_confirmed=smtp_confirmed,
            )
            return ProviderResult(
                status=status, sub_status=sub_status,
                is_disposable=is_disposable, is_role=is_role, is_free=is_free,
                mx_found=mx_found, raw=raw or {"domain": domain},
                score=score, reason_codes=codes,
                canonical=canonical, suggestion=suggestion,
            )

        # Cheap string rejections BEFORE any DNS / SMTP / paid API call —
        # each one saves a lookup or a Bouncify credit.
        if is_reserved_domain(domain):
            codes.append("RESERVED_DOMAIN")
            return _result("invalid", "reserved_domain")

        if has_mixed_script(domain):
            codes.append("MIXED_SCRIPT")
            return _result("invalid", "mixed_script_domain")

        if has_confusables(domain):
            codes.append("CONFUSABLE_DOMAIN")
            return _result("invalid", "confusable_domain")

        if not is_known_tld(domain):
            codes.append("BAD_TLD")
            return _result("invalid", "bad_tld")

        # Curated typo set — zero-false-positive fast path.
        if domain in _TYPO_DOMAINS:
            codes.append("TYPO_DOMAIN")
            return _result(
                "invalid", "typo_domain",
                raw={"domain": domain, "reason": "common typo of a major free-mail provider"},
            )

        # Edit-distance typo (long tail): annotate + suggest a fix, but
        # let the MX check decide validity — a distance-1 neighbour of
        # gmail.com can still be a real company domain.
        target = suggest_domain(domain)
        if target:
            codes.append("TYPO_SUSPECTED")
            suggestion = f"{local_part}@{target}"

        if is_disposable:
            codes.append("DISPOSABLE")
            return _result("risky", "disposable")

        # 2. MX lookup — trinary result distinguishes NXDOMAIN (invalid)
        #    from SERVFAIL/timeout (unknown, retry-worthy). Also detects
        #    RFC 7505 null MX and A-record fallback.
        mx = await _check_mx(domain)
        if mx.get("null_mx"):
            codes.append("NULL_MX")
            return _result("invalid", "null_mx", raw={"domain": domain})
        if mx.get("nxdomain"):
            codes.append("NXDOMAIN")
            return _result("invalid", "nxdomain", raw={"domain": domain})
        if mx.get("transient"):
            # DNS SERVFAIL / timeout — verdict cannot be trusted either
            # way. Let the retry_unknowns.py workflow re-check later.
            codes.append("DNS_TRANSIENT")
            return ProviderResult(
                status="unknown", sub_status="dns_transient",
                is_disposable=is_disposable, is_role=is_role, is_free=is_free,
                mx_found=False, raw={"domain": domain}, score=None,
                reason_codes=codes, canonical=canonical, suggestion=suggestion,
            )
        if not mx.get("found"):
            codes.append("NO_MX")
            return _result("invalid", "no_mx", raw={"domain": domain})

        auth_raw: dict = {"domain": domain}
        if mx.get("via_a"):
            # RFC 5321 permits A-record fallback but parked / for-sale
            # domains routinely serve A without a working mailserver.
            # Flag it so downstream scoring can downgrade.
            codes.append("MX_FROM_A")
            auth_raw["mx_from_a"] = True

        # SPF / DMARC / MTA-STS — optional, cached per domain
        if settings.enable_spf_dmarc_check:
            auth = await _check_domain_auth(domain)
            auth_raw.update(auth)
            if not auth.get("has_spf"):
                codes.append("NO_SPF")
            if not auth.get("has_dmarc"):
                codes.append("NO_DMARC")
            if auth.get("has_mta_sts"):
                auth_raw["has_mta_sts"] = True

        # 3. SMTP probe (optional)
        if settings.enable_smtp_probe:
            # Catch-all first — a domain that accepts every random local
            # can never confirm the target mailbox, so a 250 tells us
            # nothing. Downgrade to risky and skip the target probe.
            if settings.enable_catch_all_probe and await _is_catch_all(domain):
                codes.append("CATCH_ALL")
                return _result("risky", "catch_all", mx_found=True, raw=auth_raw)

            smtp_status = await _smtp_probe(normalized, domain)
            if smtp_status == "valid":
                return _result(
                    "valid", "smtp_confirmed",
                    mx_found=True, smtp_confirmed=True,
                    raw={**auth_raw, "smtp": "250"},
                )
            elif smtp_status == "invalid":
                codes.append("SMTP_REJECTED")
                return _result(
                    "invalid", "smtp_rejected", mx_found=True,
                    raw={**auth_raw, "smtp": "550"},
                )

        if is_noreply:
            return _result("risky", "no_reply", mx_found=True, raw=auth_raw)
        sub = "role_based" if is_role else ("free_provider" if is_free else "")
        return _result("valid", sub, mx_found=True, raw=auth_raw)

    async def verify_bulk(self, emails: list[str]) -> list[ProviderResult]:
        results = await asyncio.gather(*[self.verify(e) for e in emails])
        return list(results)


async def _check_mx(domain: str) -> dict[str, bool]:
    """MX resolution with verdict granularity that popular tools use.

    Returns a dict with:
      found      — deliverable MX (or A-record fallback) exists
      via_a      — MX absent, resolved via A/AAAA per RFC 5321
      nxdomain   — domain provably does not exist (invalid)
      transient  — SERVFAIL / timeout / no-nameservers (unknown, retry)
      null_mx    — RFC 7505 null MX ("MX 0 .") — domain refuses mail
    """
    state = {
        "found": False, "via_a": False,
        "nxdomain": False, "transient": False, "null_mx": False,
    }
    try:
        answers = await dns.asyncresolver.resolve(domain, "MX")
        # RFC 7505: a single MX with preference 0 and root ("." / empty)
        # exchange means the domain does not accept mail. Explicit hard
        # negative — treat as invalid, not risky.
        rrs = list(answers)
        if len(rrs) == 1:
            rr = rrs[0]
            exchange = str(rr.exchange).rstrip(".").strip()
            if rr.preference == 0 and exchange == "":
                state["null_mx"] = True
                return state
        state["found"] = True
        return state
    except dns.resolver.NXDOMAIN:
        state["nxdomain"] = True
        return state
    except dns.resolver.NoAnswer:
        pass  # fall through to A lookup
    except (dns.resolver.NoNameservers, dns.exception.Timeout):
        state["transient"] = True
        return state
    except Exception:
        pass  # fall through to A lookup
    try:
        await dns.asyncresolver.resolve(domain, "A")
        state["found"] = True
        state["via_a"] = True
    except dns.resolver.NXDOMAIN:
        state["nxdomain"] = True
    except (dns.resolver.NoNameservers, dns.exception.Timeout):
        state["transient"] = True
    except Exception:
        pass
    return state


async def _check_domain_auth(domain: str) -> dict[str, bool]:
    """SPF + DMARC (with organizational-domain fallback per RFC 7489
    § 6.6.3) + MTA-STS presence. All results cached per domain."""
    cached = _cache_get(_AUTH_CACHE, domain)
    if cached is not None:
        return cached
    has_spf = await _has_txt_prefix(domain, "v=spf1")
    has_dmarc = await _has_txt_prefix(f"_dmarc.{domain}", "v=dmarc1")
    if not has_dmarc:
        org = registrable_domain(domain)
        if org and org != domain:
            has_dmarc = await _has_txt_prefix(f"_dmarc.{org}", "v=dmarc1")
    has_mta_sts = await _has_txt_prefix(f"_mta-sts.{domain}", "v=stsv1")
    result = {"has_spf": has_spf, "has_dmarc": has_dmarc, "has_mta_sts": has_mta_sts}
    _cache_set(_AUTH_CACHE, domain, result)
    return result


async def _has_txt_prefix(name: str, prefix: str) -> bool:
    """True if any TXT record at `name` starts with `prefix` (case-insensitive).
    SPF/DMARC records can be split into multiple string chunks; we
    concatenate before matching per RFC 7208 § 3.3."""
    try:
        answers = await dns.asyncresolver.resolve(name, "TXT")
    except Exception:
        return False
    for rr in answers:
        try:
            txt = b"".join(rr.strings).decode("utf-8", "ignore").lower()
        except Exception:
            continue
        if txt.startswith(prefix.lower()):
            return True
    return False


async def _mx_host(domain: str) -> str | None:
    try:
        answers = await dns.asyncresolver.resolve(domain, "MX")
        return str(sorted(answers, key=lambda r: r.preference)[0].exchange).rstrip(".")
    except Exception:
        return None


async def _is_public_ip(host: str) -> bool:
    """B1 fix — async A-record lookup; the previous socket.gethostbyname
    blocked the event loop for every catch-all/target probe."""
    try:
        answers = await dns.asyncresolver.resolve(host, "A")
    except Exception:
        return False
    for rr in answers:
        try:
            ip = ipaddress.ip_address(rr.address)
        except Exception:
            continue
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            return True
    return False


def _helo_domain() -> str:
    """B5 fix — never announce a reserved TLD in HELO/EHLO. Prefer the
    configured smtp_helo_domain, then the domain of smtp_probe_from,
    then a safe generic fallback."""
    if settings.smtp_helo_domain:
        return settings.smtp_helo_domain
    if "@" in settings.smtp_probe_from:
        return settings.smtp_probe_from.split("@", 1)[1]
    return "mail.example.com"


async def _rcpt_probe(mx_host: str, email: str) -> tuple[str, int | None]:
    """Single EHLO/MAIL/RCPT round trip. Returns (verdict, smtp_code).
    verdict is valid | invalid | unknown; code is None on connect error."""
    try:
        smtp = aiosmtplib.SMTP(hostname=mx_host, port=25, timeout=10)
        await smtp.connect()
        try:
            try:
                await smtp.ehlo(_helo_domain())
            except Exception:
                await smtp.helo(_helo_domain())
            await smtp.mail(settings.smtp_probe_from)
            code, _ = await smtp.rcpt(email)
        finally:
            try:
                await smtp.quit()
            except Exception:
                pass
        if code == 250:
            return "valid", code
        if code == 550:
            return "invalid", code
        return "unknown", code
    except Exception:
        return "unknown", None


async def _rcpt_probe_with_retry(mx_host: str, email: str) -> str:
    """D1 — greylist retry on 4xx. Kept behind smtp_greylist_retry so
    bulk mode (where retry_unknowns.py handles the retry sweep) doesn't
    pay the sleep."""
    verdict, code = await _rcpt_probe(mx_host, email)
    if (
        settings.smtp_greylist_retry
        and code is not None
        and 400 <= code < 500
    ):
        await asyncio.sleep(settings.smtp_greylist_sleep)
        verdict, _ = await _rcpt_probe(mx_host, email)
    return verdict


async def _is_catch_all(domain: str) -> bool:
    """Probe with a random local-part; if the MX still accepts, the
    domain is catch-all. Result cached per domain (bounded LRU)."""
    cached = _cache_get(_CATCH_ALL_CACHE, domain)
    if cached is not None:
        return cached
    mx_host = await _mx_host(domain)
    if not mx_host or not await _is_public_ip(mx_host):
        return False
    # D2 — plain random string, no "probe-*" prefix that anti-spam
    # heuristics fingerprint bulk verifiers by.
    random_local = f"{secrets.token_urlsafe(12).lower().replace('_', '').replace('-', '')}@{domain}"
    verdict = await _rcpt_probe_with_retry(mx_host, random_local)
    is_catch_all = verdict == "valid"
    _cache_set(_CATCH_ALL_CACHE, domain, is_catch_all)
    return is_catch_all


async def _smtp_probe(email: str, domain: str) -> str:
    if _cache_get(_CATCH_ALL_CACHE, domain):
        return "unknown"
    mx_host = await _mx_host(domain)
    if not mx_host or not await _is_public_ip(mx_host):
        return "unknown"
    return await _rcpt_probe_with_retry(mx_host, email)
