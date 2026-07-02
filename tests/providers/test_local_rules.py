from unittest.mock import patch

import pytest

from app.providers.local import LocalProvider
from app.providers.local_rules import (
    SCORE_WEIGHTS,
    _damerau_levenshtein,
    canonicalize,
    compute_score,
    has_mixed_script,
    has_suspicious_pattern,
    is_known_tld,
    is_reserved_domain,
    strip_invisible,
    suggest_domain,
)

# ---------------------------------------------------------------- unit level

def test_canonicalize_gmail_dots_and_plus():
    assert canonicalize("John.Doe+news", "gmail.com") == "johndoe@gmail.com"


def test_canonicalize_alias_domain_fold():
    assert canonicalize("user", "googlemail.com") == "user@gmail.com"
    assert canonicalize("user+x", "protonmail.com") == "user@proton.me"


def test_canonicalize_dots_kept_outside_gmail():
    assert canonicalize("john.doe", "outlook.com") == "john.doe@outlook.com"


def test_strip_invisible():
    cleaned, flagged = strip_invisible("user​@gmail.com")
    assert cleaned == "user@gmail.com"
    assert flagged is True
    cleaned, flagged = strip_invisible("user@gmail.com")
    assert flagged is False


def test_reserved_domains():
    assert is_reserved_domain("example.com")
    assert is_reserved_domain("foo.test")
    assert is_reserved_domain("bar.invalid")
    assert not is_reserved_domain("edstellar.com")


def test_known_tld():
    assert is_known_tld("gmail.com")
    assert is_known_tld("company.co.uk")
    assert not is_known_tld("gmail.conn")
    assert not is_known_tld("foo.notarealtld")


def test_mixed_script_detection():
    # 'ра' are Cyrillic — classic paypal homoglyph
    assert has_mixed_script("раypal.com")
    assert not has_mixed_script("paypal.com")
    # single-script IDN is legitimate
    assert not has_mixed_script("bücher.de")


def test_damerau_levenshtein_transposition():
    assert _damerau_levenshtein("gmial.com", "gmail.com") == 1
    assert _damerau_levenshtein("gmail.com", "gmail.com") == 0


def test_suggest_domain():
    assert suggest_domain("gmajl.com") == "gmail.com"
    # allowlisted legit neighbours never flagged
    assert suggest_domain("mail.com") is None
    assert suggest_domain("ymail.com") is None
    # exact matches never flagged
    assert suggest_domain("gmail.com") is None


def test_suspicious_patterns():
    assert has_suspicious_pattern("123456789012")
    assert has_suspicious_pattern("aaaaaaaaaa")
    assert has_suspicious_pattern("qwerty9876")
    assert not has_suspicious_pattern("john.smith")


def test_score_weights_sum_to_100():
    assert sum(SCORE_WEIGHTS.values()) == 100


def test_score_caps_without_smtp():
    score = compute_score([], is_free=True, mx_found=True, smtp_confirmed=False)
    assert score == 95  # local checks never prove deliverability


# ------------------------------------------------------------ provider level

@pytest.mark.asyncio
async def test_reserved_domain_rejected_before_dns():
    provider = LocalProvider()
    with patch("app.providers.local._check_mx") as mx:
        result = await provider.verify("user@example.com")
    mx.assert_not_called()
    assert result.status == "invalid"
    assert result.sub_status == "reserved_domain"
    assert "RESERVED_DOMAIN" in result.reason_codes
    assert result.score == 0


@pytest.mark.asyncio
async def test_bad_tld_rejected_before_dns():
    provider = LocalProvider()
    with patch("app.providers.local._check_mx") as mx:
        result = await provider.verify("user@company.notarealtld")
    mx.assert_not_called()
    assert result.sub_status == "bad_tld"


@pytest.mark.asyncio
async def test_mixed_script_rejected():
    provider = LocalProvider()
    result = await provider.verify("user@раypal.com")
    assert result.status == "invalid"
    assert result.sub_status == "mixed_script_domain"


@pytest.mark.asyncio
async def test_typo_suspected_annotates_with_suggestion():
    provider = LocalProvider()
    with patch("app.providers.local._check_mx", return_value={"found": False}):
        result = await provider.verify("user@gmajl.com")
    assert result.status == "invalid"  # MX decided, not the typo check
    assert "TYPO_SUSPECTED" in result.reason_codes
    assert result.suggestion == "user@gmail.com"


@pytest.mark.asyncio
async def test_noreply_flagged_risky():
    provider = LocalProvider()
    with patch("app.providers.local._check_mx", return_value={"found": True}):
        result = await provider.verify("noreply@edstellar.com")
    assert result.status == "risky"
    assert result.sub_status == "no_reply"
    assert "NO_REPLY" in result.reason_codes


@pytest.mark.asyncio
async def test_canonical_field_on_result():
    provider = LocalProvider()
    with patch("app.providers.local._check_mx", return_value={"found": True}):
        result = await provider.verify("John.Doe+tag@gmail.com")
    assert result.canonical == "johndoe@gmail.com"


@pytest.mark.asyncio
async def test_invisible_chars_stripped_and_flagged():
    provider = LocalProvider()
    with patch("app.providers.local._check_mx", return_value={"found": True}):
        result = await provider.verify("user​@gmail.com")
    assert result.status == "valid"
    assert "INVISIBLE_CHARS" in result.reason_codes


@pytest.mark.asyncio
async def test_valid_corporate_email_scored():
    provider = LocalProvider()
    with patch("app.providers.local._check_mx", return_value={"found": True}):
        result = await provider.verify("john.smith@edstellar.com")
    assert result.status == "valid"
    assert result.score == 90  # everything except known_provider + smtp_confirmed
    assert result.reason_codes == []
