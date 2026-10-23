import pytest

from app.services.url_validation import InvalidURLError, URLPolicy, validate_target_url

POLICY = URLPolicy(max_length=2048, own_hosts=frozenset({"sho.rt"}))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com", "https://example.com"),
        ("  https://Example.COM/Path?Q=1#Frag  ", "https://example.com/Path?Q=1#Frag"),
        ("HTTP://www.python.org:8080/docs", "http://www.python.org:8080/docs"),
        ("https://bücher.de/katalog", "https://xn--bcher-kva.de/katalog"),
        ("https://8.8.8.8/dns", "https://8.8.8.8/dns"),
        ("https://[2606:4700:4700::1111]/", "https://[2606:4700:4700::1111]/"),
        ("https://sub_domain.example.org/", "https://sub_domain.example.org/"),
        ("https://example.com./trailing-dot", "https://example.com/trailing-dot"),
    ],
)
def test_accepts_and_normalises_public_urls(raw: str, expected: str) -> None:
    assert validate_target_url(raw, POLICY) == expected


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("", "empty"),
        ("ftp://example.com/file", "Only http and https"),
        ("javascript:alert(1)", "Only http and https"),
        ("example.com", "Only http and https"),
        ("https://", "hostname"),
        ("https://exa mple.com", "whitespace"),
        ("https://example.com/\x00", "control"),
        ("https://user:pass@example.com", "credentials"),
        ("https://paypal.com@evil.example.net", "credentials"),
        ("http://localhost:8000/admin", "Single-label"),
        ("http://app.localhost/", "Internal"),
        ("http://printer.local/", "Internal"),
        ("http://db.internal/", "Internal"),
        ("http://intranet/", "Single-label"),
        ("http://127.0.0.1/", "private"),
        ("http://10.1.2.3/", "private"),
        ("http://172.16.0.1/", "private"),
        ("http://192.168.1.1/router", "private"),
        ("http://169.254.169.254/latest/meta-data", "private"),
        ("http://100.64.0.1/", "private"),
        ("http://0.0.0.0/", "private"),
        ("http://224.0.0.1/", "private"),
        ("http://[::1]/", "private"),
        ("http://[fd00::1]/", "private"),
        ("http://[::ffff:127.0.0.1]/", "private"),
        ("http://2130706433/", "private"),
        ("http://0x7f.1/", "private"),
        ("http://127.1/", "private"),
        ("https://sho.rt/abc", "shortener itself"),
        ("https://example.com:99999/", "malformed"),
        ("https://-bad-.com/", "malformed"),
        ("https://exa$mple.com/", "invalid characters"),
        ("https://example.123/", "top-level"),
        ("https://" + "a" * 64 + ".com/", "malformed"),
        ("https://" + ("a" * 60 + ".") * 5 + "com/", "too long"),
        ("https://\ufffd.com/", "internationalised"),
    ],
)
def test_rejects_unsafe_or_malformed_urls(raw: str, reason: str) -> None:
    with pytest.raises(InvalidURLError, match=reason):
        validate_target_url(raw, POLICY)


def test_rejects_urls_over_the_length_limit() -> None:
    policy = URLPolicy(max_length=30, own_hosts=frozenset())
    with pytest.raises(InvalidURLError, match="at most 30"):
        validate_target_url("https://example.com/" + "a" * 20, policy)
