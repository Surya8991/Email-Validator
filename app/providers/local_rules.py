"""Offline validation rules for LocalProvider.

Pure string/set checks — no network. Everything here runs BEFORE the
DNS/SMTP path in local.py so garbage never costs a lookup or a paid
API credit.
"""

import unicodedata
from pathlib import Path

import tldextract

# Bundled PSL snapshot only — no network fetch at runtime. Keeps
# `is_reserved_domain` / `is_known_tld` correct on multi-level TLDs
# (`.co.uk`, `.com.au`, `github.io`) where `rsplit(".", 1)[-1]` lies.
_TLD_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def _top_tld(domain: str) -> str:
    """Right-most label of the public suffix (e.g. `uk` for `co.uk`)."""
    suffix = _TLD_EXTRACT(domain).suffix
    if suffix:
        return suffix.rsplit(".", 1)[-1].lower()
    return domain.rsplit(".", 1)[-1].lower() if "." in domain else domain.lower()


def registrable_domain(domain: str) -> str:
    """Return the eTLD+1 (`foo.co.uk` from `mail.foo.co.uk`), or the input
    unchanged if the suffix isn't in the bundled PSL."""
    ext = _TLD_EXTRACT(domain)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}".lower()
    return domain.lower()

# ---------------------------------------------------------------------------
# Reserved / special-use domains (RFC 2606 + RFC 6761 + RFC 6762).
# Syntactically valid, never publicly deliverable.
# ---------------------------------------------------------------------------
_RESERVED_DOMAINS = {"example.com", "example.org", "example.net", "example.edu"}
_RESERVED_TLDS = {"test", "invalid", "localhost", "local", "example", "internal", "onion"}

# ---------------------------------------------------------------------------
# Never-read senders. Distinct from role accounts: a human may answer
# sales@, nobody answers noreply@. Hard negative for a marketing list.
# ---------------------------------------------------------------------------
NOREPLY_PREFIXES = {
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "do_not_reply", "dont-reply", "mailer-daemon", "mailerdaemon",
    "bounce", "bounces", "auto-reply", "autoreply", "autoresponder",
}

# ---------------------------------------------------------------------------
# Zero-width / directional-override characters. Survive .strip(), break
# delivery, occasionally deliberate filter evasion.
# ---------------------------------------------------------------------------
_INVISIBLE_CHARS = "​‌‍⁠﻿­‪‫‬‭‮"
_INVISIBLE_TABLE = str.maketrans("", "", _INVISIBLE_CHARS)

# ---------------------------------------------------------------------------
# IANA TLD snapshot (app/data/tlds.txt, from
# https://data.iana.org/TLD/tlds-alpha-by-domain.txt — refresh with
# scripts/fetch_tlds.py). Empty set disables the check rather than
# rejecting everything.
# ---------------------------------------------------------------------------
_TLD_FILE = Path(__file__).resolve().parent.parent / "data" / "tlds.txt"


def _load_tlds() -> set[str]:
    try:
        lines = _TLD_FILE.read_text(encoding="ascii").splitlines()
        return {ln.strip().lower() for ln in lines if ln and not ln.startswith("#")}
    except Exception:
        return set()


_VALID_TLDS = _load_tlds()

# ---------------------------------------------------------------------------
# Canonicalization — same-mailbox aliases folded to one form so bulk
# dedup catches user+tag@googlemail.com == user@gmail.com.
# ---------------------------------------------------------------------------
_ALIAS_DOMAINS = {
    "googlemail.com": "gmail.com",
    "ymail.com": "yahoo.com",
    "rocketmail.com": "yahoo.com",
    "protonmail.com": "proton.me",
    "protonmail.ch": "proton.me",
    "pm.me": "proton.me",
    "me.com": "icloud.com",
    "mac.com": "icloud.com",
}

# Providers where +tag routes to the base mailbox.
_PLUS_TAG_DOMAINS = {
    "gmail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
    "icloud.com", "proton.me", "fastmail.com",
}

# Gmail ignores dots in the local part.
_DOT_INSENSITIVE_DOMAINS = {"gmail.com"}


def canonicalize(local_part: str, domain: str) -> str:
    """Return the canonical same-mailbox form of an address."""
    local = local_part.lower()
    domain = _ALIAS_DOMAINS.get(domain, domain)
    if domain in _PLUS_TAG_DOMAINS and "+" in local:
        local = local.split("+", 1)[0]
    if domain in _DOT_INSENSITIVE_DOMAINS:
        local = local.replace(".", "")
    return f"{local}@{domain}"


def strip_invisible(email: str) -> tuple[str, bool]:
    """Remove zero-width/bidi chars. Returns (cleaned, had_invisible)."""
    cleaned = email.translate(_INVISIBLE_TABLE)
    return cleaned, cleaned != email


def is_reserved_domain(domain: str) -> bool:
    if domain in _RESERVED_DOMAINS:
        return True
    return _top_tld(domain) in _RESERVED_TLDS


def is_known_tld(domain: str) -> bool:
    """True if TLD is in the IANA snapshot (or the snapshot is missing).

    Uses the Public Suffix List to peel off multi-level TLDs before the
    IANA check — `foo.co.uk` correctly reduces to `uk`, not `co.uk`."""
    if not _VALID_TLDS:
        return True
    tld = _top_tld(domain)
    if not tld.isascii():
        try:
            tld = tld.encode("idna").decode("ascii")
        except Exception:
            return False
    return tld in _VALID_TLDS


# Hoisted to module scope — the previous per-call import was benign but
# ran on every validated email in bulk mode.
try:
    from confusable_homoglyphs import confusables as _confusables
    _HAS_CONFUSABLES = True
except Exception:
    _confusables = None
    _HAS_CONFUSABLES = False


def has_confusables(domain: str) -> bool:
    """TR39 confusables check — catches intra-script homoglyphs
    (`rn`→`m`, `0`→`O`) that `has_mixed_script` misses. Degrades to
    `False` when the confusable-homoglyphs package is unavailable."""
    if not _HAS_CONFUSABLES:
        return False
    for label in domain.split("."):
        try:
            if _confusables.is_dangerous(label):
                return True
        except Exception:
            continue
    return False


def has_mixed_script(domain: str) -> bool:
    """Detect Latin mixed with Cyrillic/Greek in a single label — the
    classic homoglyph spoof (`раypal.com`). Pure single-script IDN
    domains (`bücher.de`) pass."""
    for label in domain.split("."):
        scripts = set()
        for ch in label:
            if not ch.isalpha():
                continue
            name = unicodedata.name(ch, "")
            if name.startswith("LATIN"):
                scripts.add("latin")
            elif name.startswith("CYRILLIC"):
                scripts.add("cyrillic")
            elif name.startswith("GREEK"):
                scripts.add("greek")
        if len(scripts) > 1:
            return True
    return False


# ---------------------------------------------------------------------------
# Edit-distance typo detection. The static _TYPO_DOMAINS set in local.py
# stays the zero-false-positive fast path; this catches the long tail at
# distance 1 only (conservative — distance 2 hits real domains).
# ---------------------------------------------------------------------------
_TYPO_TARGETS = [
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "aol.com", "live.com", "proton.me", "protonmail.com", "comcast.net",
    "rediffmail.com", "zoho.com",
]

# Legit domains within distance 1 of a target — never flag these.
_TYPO_ALLOWLIST = {
    "mail.com", "email.com", "ymail.com", "gmx.com", "aim.com",
    "mail.ru", "inbox.com", "att.net",
}


def _damerau_levenshtein(a: str, b: str, cap: int = 2) -> int:
    """Optimal-string-alignment distance, early-exit above `cap`."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and ca == b[j - 2] and cb == a[i - 1 - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > cap:
            return cap + 1
        prev2, prev = prev, cur
    return prev[-1]


def suggest_domain(domain: str) -> str | None:
    """Closest major provider at edit distance 1, or None."""
    if domain in _TYPO_ALLOWLIST or domain in _TYPO_TARGETS:
        return None
    for target in _TYPO_TARGETS:
        if _damerau_levenshtein(domain, target, cap=1) <= 1:
            return target
    return None


# ---------------------------------------------------------------------------
# Suspicious local-part patterns — not invalid, just low-quality signal.
# ---------------------------------------------------------------------------
_KEYBOARD_WALKS = ("qwerty", "asdfgh", "zxcvbn", "qazwsx", "123456", "abcdef")


def has_suspicious_pattern(local_part: str) -> bool:
    lp = local_part.lower()
    if len(lp) >= 8 and lp.isdigit():
        return True
    if any(walk in lp for walk in _KEYBOARD_WALKS):
        return True
    # A single character repeated 6+ times in a row
    run, prev = 1, ""
    for ch in lp:
        run = run + 1 if ch == prev else 1
        if run >= 6:
            return True
        prev = ch
    return False


# ---------------------------------------------------------------------------
# Scoring. Weights sum to 100; single source of truth for docs and code.
# Local checks can prove an address invalid but never deliverable, so
# only an SMTP-confirmed result reaches 100 (no_typo/no_pattern still
# counted) — everything else caps at 95 via the smtp_confirmed weight.
# ---------------------------------------------------------------------------
SCORE_WEIGHTS = {
    "valid_syntax": 35,
    "valid_domain_format": 20,
    "known_provider": 5,
    "not_disposable": 15,
    "not_role": 5,
    "no_typo": 5,
    "no_suspicious_pattern": 5,
    "mx_found": 5,
    "smtp_confirmed": 5,
}
assert sum(SCORE_WEIGHTS.values()) == 100


def compute_score(reason_codes: list[str], *, is_free: bool, mx_found: bool,
                  smtp_confirmed: bool) -> int:
    """Score from the same signals that produced the reason codes."""
    codes = set(reason_codes)
    if "SYNTAX_ERROR" in codes:
        return 0
    score = SCORE_WEIGHTS["valid_syntax"]
    if not codes & {"RESERVED_DOMAIN", "BAD_TLD", "MIXED_SCRIPT", "TYPO_DOMAIN"}:
        score += SCORE_WEIGHTS["valid_domain_format"]
    if is_free:
        score += SCORE_WEIGHTS["known_provider"]
    if "DISPOSABLE" not in codes:
        score += SCORE_WEIGHTS["not_disposable"]
    if not codes & {"ROLE_ACCOUNT", "NO_REPLY"}:
        score += SCORE_WEIGHTS["not_role"]
    if not codes & {"TYPO_DOMAIN", "TYPO_SUSPECTED"}:
        score += SCORE_WEIGHTS["no_typo"]
    if "SUSPICIOUS_PATTERN" not in codes:
        score += SCORE_WEIGHTS["no_suspicious_pattern"]
    if mx_found:
        score += SCORE_WEIGHTS["mx_found"]
    if smtp_confirmed:
        score += SCORE_WEIGHTS["smtp_confirmed"]
    return score
