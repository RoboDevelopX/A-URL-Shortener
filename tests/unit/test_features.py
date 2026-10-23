import pytest

from app.ml.features import (
    FEATURE_NAMES,
    extract_features,
    featurize,
    normalize_url,
    registered_domain,
)


@pytest.mark.parametrize(
    ("url", "host", "port", "rest"),
    [
        ("https://www.Example.com/a?b=1", "example.com", None, "/a?b=1"),
        ("example.com", "example.com", None, "/"),
        ("http://example.com", "example.com", None, "/"),
        ("http://example.com?q=1", "example.com", None, "/?q=1"),
        ("http://user@evil.com:8080/x", "evil.com", "8080", "/x"),
        ("HTTP://EXAMPLE.COM./", "example.com", None, "/"),
    ],
)
def test_normalize_url(url: str, host: str, port: str | None, rest: str) -> None:
    parts = normalize_url(url)
    assert (parts.host, parts.port, parts.rest) == (host, port, rest)


def test_scheme_www_and_trailing_slash_do_not_change_features() -> None:
    # These differences were label shortcuts in the training data; they must be invisible.
    variants = ["example.com", "https://example.com", "http://www.example.com/"]
    assert len({tuple(extract_features(v).items()) for v in variants}) == 1


@pytest.mark.parametrize(
    ("host", "domain"),
    [
        ("a.b.example.com", "example.com"),
        ("shop.example.co.uk", "example.co.uk"),
        ("example.com", "example.com"),
        ("10.0.0.1", "10.0.0.1"),
        ("localhost", "localhost"),
    ],
)
def test_registered_domain(host: str, domain: str) -> None:
    assert registered_domain(host) == domain


def test_phishing_shaped_url_features() -> None:
    features = extract_features(
        "http://paypal.com.secure-login.xyz/webscr/cmd=_login/update.php?a=1&b=2"
    )
    assert features["tld"] == "xyz"
    assert features["subdomain_count"] == 2
    assert features["brand_outside_domain"] == 1.0
    assert features["host_hyphens"] == 1
    assert features["query_params"] == 2
    assert features["path_depth"] == 3
    assert float(features["suspicious_word_count"]) >= 4
    assert features["is_ip_host"] == 0.0


def test_brand_on_its_own_domain_is_not_flagged() -> None:
    assert extract_features("https://www.paypal.com/signin")["brand_outside_domain"] == 0.0


def test_ip_host_features() -> None:
    features = extract_features("http://198.51.100.23:8080/bins/mirai.x86")
    assert features["is_ip_host"] == 1.0
    assert features["has_port"] == 1.0
    assert features["tld"] == "<ip>"
    assert features["subdomain_count"] == 0


@pytest.mark.parametrize(
    ("url", "feature", "value"),
    [
        ("https://example.com/setup.exe", "risky_extension", 1.0),
        ("https://bit.ly/abc", "is_shortener", 1.0),
        ("https://xn--pypal-4ve.com/", "is_punycode", 1.0),
        ("https://example.com/a//b", "has_double_slash_in_path", 1.0),
        ("https://example.com/?u=a%40b", "percent_encoded", 1),
        ("https://bcdfghjk.com/", "host_max_consonant_run", 8),
        ("https://intranet/", "tld", "<none>"),
    ],
)
def test_individual_features(url: str, feature: str, value: object) -> None:
    assert extract_features(url)[feature] == value


def test_ratios_and_entropy_are_bounded() -> None:
    features = extract_features("https://example.com/AbC123")
    for name in ("digit_ratio", "special_char_ratio", "uppercase_ratio"):
        assert 0.0 <= float(features[name]) <= 1.0
    assert float(features["url_entropy"]) > 0
    assert extract_features("a")["host_entropy"] == 0.0


def test_featurize_returns_columns_in_feature_order() -> None:
    matrix = featurize(["https://example.com/", "http://evil.top/login"])
    assert matrix.shape == (2, len(FEATURE_NAMES))
    assert matrix[1, FEATURE_NAMES.index("tld")] == "top"
