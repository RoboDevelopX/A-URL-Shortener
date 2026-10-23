"""Validation of destination URLs.

A shortener is an open redirector by design, so it is the easiest way to disguise where a
link really points. We refuse destinations that only make sense inside a private network
(localhost, RFC 1918 ranges, ``.internal`` names): links to them are almost always mistakes,
probes, or attempts to make the shortener look like a trusted internal service.

We deliberately do *not* resolve DNS here. Resolution would add latency and a network
dependency to every create, and it proves little: DNS can change after the check (DNS
rebinding). This service never fetches the destination itself, so there is no server-side
request forgery risk to defend against; the check is about what we lend our domain to.
"""

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from app.core.errors import AppError

ALLOWED_SCHEMES = frozenset({"http", "https"})
# Special-use and de-facto internal suffixes (RFC 6761, RFC 8375, common corporate usage).
INTERNAL_SUFFIXES = (
    "localhost",
    "local",
    "localdomain",
    "internal",
    "intranet",
    "lan",
    "home",
    "corp",
    "home.arpa",
    "test",
    "invalid",
    "example",
    "onion",
)
_HOST_LABEL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")


class InvalidURLError(AppError):
    status_code = 422
    code = "invalid_url"
    default_message = "URL is not allowed."


@dataclass(frozen=True, slots=True)
class URLPolicy:
    """Inputs to validation that come from configuration."""

    max_length: int
    own_hosts: frozenset[str]


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return the IP if ``host`` is an IP literal in any notation browsers accept.

    Browsers accept legacy IPv4 forms such as ``2130706433``, ``0x7f.1`` or ``127.1`` (all
    127.0.0.1); ``inet_aton`` understands the same forms, so a decimal disguise is caught.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if all(c in "0123456789abcdefx." for c in host):
        try:
            return ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return None
    return None


def _check_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast:
        raise InvalidURLError("URLs pointing to private, loopback or reserved IPs are not allowed.")


def _check_hostname(host: str) -> None:
    if len(host) > 253:
        raise InvalidURLError("Hostname is too long.")
    labels = host.split(".")
    if len(labels) < 2:
        raise InvalidURLError("Single-label (internal) hostnames are not allowed.")
    for label in labels:
        if not 1 <= len(label) <= 63 or label[0] == "-" or label[-1] == "-":
            raise InvalidURLError("Hostname is malformed.")
        if not set(label) <= _HOST_LABEL_CHARS:
            raise InvalidURLError("Hostname contains invalid characters.")
    if not (labels[-1].isalpha() or labels[-1].startswith("xn--")):
        raise InvalidURLError("Hostname has an invalid top-level domain.")
    for suffix in INTERNAL_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            raise InvalidURLError("Internal or reserved hostnames are not allowed.")


def validate_target_url(raw_url: str, policy: URLPolicy) -> str:
    """Return a normalised copy of ``raw_url`` or raise ``InvalidURLError``.

    Normalisation lowercases the scheme and host and converts internationalised hostnames
    to punycode; path, query and fragment are preserved exactly.
    """
    url = raw_url.strip()
    if not url:
        raise InvalidURLError("URL must not be empty.")
    if len(url) > policy.max_length:
        raise InvalidURLError(f"URL must be at most {policy.max_length} characters.")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise InvalidURLError("URL must not contain whitespace or control characters.")

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise InvalidURLError("URL is malformed.") from exc

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidURLError("Only http and https URLs are allowed.")
    if parts.username is not None or parts.password is not None:
        # ``https://paypal.com@evil.example`` goes to evil.example; a classic phishing trick.
        raise InvalidURLError("URLs with embedded credentials are not allowed.")
    if not parts.hostname:
        raise InvalidURLError("URL must include a hostname.")

    host = parts.hostname.rstrip(".")
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise InvalidURLError("Hostname is not a valid internationalised name.") from exc

    ip = _parse_ip_literal(host)
    if ip is not None:
        _check_ip(ip)
        netloc_host = f"[{ip.compressed}]" if ip.version == 6 else ip.compressed
    else:
        _check_hostname(host)
        if host in policy.own_hosts:
            raise InvalidURLError("Links to this shortener itself are not allowed.")
        netloc_host = host

    netloc = netloc_host if port is None else f"{netloc_host}:{port}"
    return urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))
