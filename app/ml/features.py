"""Lexical URL features, shared by training (``ml/train.py``) and serving.

Training and serving import this one module, so the features a model was trained on are
exactly the features it sees in production. Re-implementing feature code in two places is
the classic source of train/serve skew, and it fails silently: the model still returns a
score, just a wrong one.

Only the URL string is used. No DNS, WHOIS or page fetches, so scoring takes microseconds,
needs no network access and cannot be used to make this service contact an attacker's host.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_TOKEN_SPLIT = re.compile(r"[^a-zA-Z0-9]+")
_CONSONANT_RUN = re.compile(r"[bcdfghjklmnpqrstvwxz]+")

SUSPICIOUS_WORDS = (
    "login",
    "signin",
    "logon",
    "verify",
    "verification",
    "account",
    "update",
    "secure",
    "security",
    "banking",
    "confirm",
    "password",
    "passwd",
    "webscr",
    "cmd=",
    "wp-admin",
    "wp-includes",
    "admin",
    "suspend",
    "unlock",
    "billing",
    "invoice",
    "wallet",
    "free",
    "bonus",
    "lucky",
    "gift",
    "payload",
    ".exe",
)
# Brands phishers impersonate. The feature fires when a brand appears somewhere other than
# the registered domain, e.g. ``paypal.com.account-check.xyz``.
BRANDS = (
    "paypal",
    "apple",
    "icloud",
    "microsoft",
    "office365",
    "outlook",
    "google",
    "facebook",
    "instagram",
    "amazon",
    "netflix",
    "ebay",
    "dropbox",
    "chase",
    "wellsfargo",
    "bankofamerica",
    "itau",
    "santander",
)
RISKY_EXTENSIONS = (".exe", ".scr", ".apk", ".bat", ".dll", ".bin", ".jar", ".msi", ".vbs")
SHORTENER_HOSTS = frozenset(
    {"bit.ly", "tinyurl.com", "goo.gl", "t.co", "ow.ly", "is.gd", "buff.ly", "rebrand.ly"}
)
# Second-level labels under which registrations happen (``example.co.uk``). A heuristic;
# a full public-suffix list would be more exact but adds a dependency for little gain here.
_SECOND_LEVEL = frozenset({"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go"})

NUMERIC_FEATURES = (
    "url_length",
    "host_length",
    "path_length",
    "query_length",
    "host_dots",
    "host_hyphens",
    "host_digits",
    "subdomain_count",
    "is_ip_host",
    "has_port",
    "digit_ratio",
    "special_char_ratio",
    "uppercase_ratio",
    "url_entropy",
    "host_entropy",
    "path_depth",
    "query_params",
    "has_at_sign",
    "has_double_slash_in_path",
    "percent_encoded",
    "longest_token",
    "host_max_consonant_run",
    "suspicious_word_count",
    "brand_outside_domain",
    "risky_extension",
    "is_shortener",
    "is_punycode",
)
FEATURE_NAMES = (*NUMERIC_FEATURES, "tld")


@dataclass(frozen=True, slots=True)
class SplitURL:
    host: str
    port: str | None
    rest: str  # path + query + fragment; always starts with "/"


def normalize_url(url: str) -> SplitURL:
    """Canonicalise a URL and split it into host and the rest.

    Three dataset artefacts are neutralised here, identically for training and serving.
    In the public training data, benign URLs never have a scheme and malicious ones
    sometimes do; ``www.`` appears almost only in the malicious class; and every benign
    bare domain ends in ``/`` while 2,102 malicious ones have no path at all. A model
    happily learns all three as "malicious" signals that mean nothing for real traffic
    (the first trained model scored ``https://example.com`` at 0.998). So the scheme and
    ``www.`` are dropped and an empty path becomes ``/``, which is also what RFC 3986 says
    an empty HTTP path means.
    """
    stripped = _SCHEME.sub("", url.strip(), count=1)
    cut = len(stripped)
    for sep in "/?#":
        index = stripped.find(sep)
        if index != -1:
            cut = min(cut, index)
    netloc, rest = stripped[:cut], stripped[cut:]
    netloc = netloc.rsplit("@", 1)[-1].lower()
    port = None
    if ":" in netloc and not netloc.startswith("["):
        netloc, port = netloc.rsplit(":", 1)
    host = netloc.rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not rest.startswith("/"):
        rest = "/" + rest
    return SplitURL(host=host, port=port, rest=rest)


def registered_domain(host: str) -> str:
    """Approximate registrable domain (``a.b.example.co.uk`` -> ``example.co.uk``)."""
    if _IPV4.match(host):
        return host
    labels = [label for label in host.split(".") if label]
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _entropy(text: str) -> float:
    if not text:
        return 0.0
    counts = Counter(text)
    total = len(text)
    return -sum(n / total * math.log2(n / total) for n in counts.values())


def _tld(host: str) -> str:
    if _IPV4.match(host) or host.startswith("["):
        return "<ip>"
    if "." not in host:
        return "<none>"
    return host.rsplit(".", 1)[-1]


def extract_features(url: str) -> dict[str, float | str]:
    """Map one URL to named features (numeric values plus the categorical ``tld``)."""
    parts = normalize_url(url)
    host, rest = parts.host, parts.rest
    full = host + rest
    length = max(len(full), 1)
    path, _, query = rest.partition("?")
    query = query.split("#", 1)[0]
    domain = registered_domain(host)
    subdomain_part = host[: -len(domain)] if host.endswith(domain) else host
    lowered = full.lower()
    is_ip = bool(_IPV4.match(host)) or host.startswith("[")

    features: dict[str, float | str] = {
        "url_length": len(full),
        "host_length": len(host),
        "path_length": len(path),
        "query_length": len(query),
        "host_dots": host.count("."),
        "host_hyphens": host.count("-"),
        "host_digits": sum(c.isdigit() for c in host),
        "subdomain_count": 0 if is_ip else max(host.count(".") - domain.count("."), 0),
        "is_ip_host": float(is_ip),
        "has_port": float(parts.port is not None),
        "digit_ratio": sum(c.isdigit() for c in full) / length,
        "special_char_ratio": sum(not c.isalnum() for c in full) / length,
        "uppercase_ratio": sum(c.isupper() for c in full) / length,
        "url_entropy": _entropy(full),
        "host_entropy": _entropy(host),
        "path_depth": path.count("/"),
        "query_params": query.count("&") + 1 if query else 0,
        "has_at_sign": float("@" in url),
        "has_double_slash_in_path": float("//" in rest),
        "percent_encoded": rest.count("%"),
        "longest_token": max((len(t) for t in _TOKEN_SPLIT.split(full) if t), default=0),
        "host_max_consonant_run": max(
            (len(run) for run in _CONSONANT_RUN.findall(host)), default=0
        ),
        "suspicious_word_count": sum(lowered.count(word) for word in SUSPICIOUS_WORDS),
        "brand_outside_domain": float(
            any(b in subdomain_part or b in rest.lower() for b in BRANDS)
            and not any(b in domain for b in BRANDS)
        ),
        "risky_extension": float(path.lower().endswith(RISKY_EXTENSIONS)),
        "is_shortener": float(domain in SHORTENER_HOSTS),
        "is_punycode": float("xn--" in host),
        "tld": _tld(host),
    }
    return features


def to_matrix(rows: list[dict[str, float | str]]) -> npt.NDArray[np.object_]:
    """Stack feature dicts into the column order the model pipeline expects."""
    matrix = np.empty((len(rows), len(FEATURE_NAMES)), dtype=object)
    for i, row in enumerate(rows):
        for j, name in enumerate(FEATURE_NAMES):
            matrix[i, j] = row[name]
    return matrix


def featurize(urls: list[str]) -> npt.NDArray[np.object_]:
    return to_matrix([extract_features(url) for url in urls])
