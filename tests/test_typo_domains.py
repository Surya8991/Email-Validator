"""Regression tests for the typo-domain short-circuit in LocalProvider
and the existence of expected entries in the blocklist."""
import asyncio

from app.providers.local import _TYPO_DOMAINS, LocalProvider


def test_typo_domain_short_circuits_to_invalid():
    """An email at a typo domain returns invalid via the local check
    without touching DNS / SMTP / external providers."""
    p = LocalProvider()
    res = asyncio.run(p.verify("foo@gnail.com"))
    assert res.status == "invalid"
    assert res.sub_status == "typo_domain"
    assert res.mx_found is False
    assert "typo" in (res.raw.get("reason") or "").lower()


def test_legit_yahoo_alias_not_in_typo_list():
    """ymail.com is a real Yahoo Mail alias — must NOT be in the
    blocklist (regression guard against accidental adds)."""
    assert "ymail.com" not in _TYPO_DOMAINS
    assert "googlemail.com" not in _TYPO_DOMAINS
    assert "mail.com" not in _TYPO_DOMAINS


def test_blocklist_covers_canonical_examples():
    """The user-provided examples in the original feature ask
    (gahooo.com, gnail.com, hotnail.com) are all flagged."""
    for d in ("gnail.com", "gahooo.com", "hotnail.com", "outlok.com", "iclod.com"):
        assert d in _TYPO_DOMAINS, f"{d} missing from typo blocklist"
