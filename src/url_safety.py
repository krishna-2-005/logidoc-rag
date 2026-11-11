"""
Outbound destination safety for webhooks (SSRF protection).

A webhook URL is chosen by an administrator, but the request is made by the
server, from inside the network the server sits in. Without these rules a URL
could reach the server itself, the private network, or a cloud metadata
service. Every rule is applied twice: when the URL is saved (check_url) and
again, after a fresh DNS lookup, immediately before each delivery
(resolve_destination), because a DNS answer can change between the two.

    scheme          https only. http only outside production and only when
                    LOGIDOC_WEBHOOK_ALLOW_HTTP=1 (a local test receiver)
    credentials     user:password@ in the URL is refused (secrets do not
                    belong in a URL, and it would be logged)
    host names      localhost, *.localhost, *.local, *.internal, *.home.arpa and
                    the cloud metadata names are refused before any lookup
    addresses       every address the name resolves to must be a public,
                    global unicast address: loopback, private (RFC 1918, ULA),
                    link-local (169.254.0.0/16, which holds the metadata
                    service), carrier-grade NAT, multicast, reserved,
                    unspecified, and IPv6 forms that embed an IPv4 address
                    (mapped, 6to4, NAT64, Teredo) are refused. One refused
                    address refuses the whole name (no round-robin trick)
    ports           443, or 1024-65535 (80 too when http is allowed); the
                    well-known ports of internal services are refused
    pinning         the connection is made to the address that was checked
                    (resolve_destination returns it), never to a second
                    lookup, so DNS rebinding cannot swap it afterwards
    redirects       never followed (src/webhooks.py treats 3xx as a failure)

LOGIDOC_WEBHOOK_ALLOW_PRIVATE=1 lifts the address rules outside production,
for a receiver on the same machine during development; production refuses to
start with it (src/environment.py).
"""

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".lan", ".intranet")
BLOCKED_NAMES = ("localhost", "metadata", "metadata.google.internal", "metadata.goog", "instance-data",
                 "instance-data.ec2.internal")

# IPv6 ranges that carry an IPv4 address inside, or are not reachable on the Internet
EMBEDDED_V6 = (
    ipaddress.ip_network("64:ff9b::/96"),     # NAT64
    ipaddress.ip_network("64:ff9b:1::/48"),   # local-use NAT64
    ipaddress.ip_network("2002::/16"),        # 6to4
    ipaddress.ip_network("2001::/32"),        # Teredo
)

MAX_URL_LENGTH = 2000


class UnsafeDestination(ValueError):
    """The destination is refused; the message says why, without echoing the URL."""


@dataclass(frozen=True)
class Policy:
    """allow_http and allow_private are for development only (never set in production)."""

    allow_http: bool = False
    allow_private: bool = False

    @staticmethod
    def from_env(environ, production):

        def on(name):
            return str(environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")

        if production:
            return Policy()

        return Policy(allow_http=on("LOGIDOC_WEBHOOK_ALLOW_HTTP"), allow_private=on("LOGIDOC_WEBHOOK_ALLOW_PRIVATE"))


@dataclass(frozen=True)
class Destination:
    """A checked destination: connect to address, present host (Host header, TLS name)."""

    scheme: str
    host: str
    port: int
    path: str
    address: str


def address_problem(address):
    """Why an IP address is refused, or None when it is a public unicast address."""

    ip = ipaddress.ip_address(address)

    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return address_problem(str(ip.ipv4_mapped))
        if any(ip in network for network in EMBEDDED_V6):
            return "an IPv6 address that embeds an IPv4 address"
        if ip.scope_id:
            return "a scoped (link-local) address"

    if ip.is_loopback:
        return "a loopback address"
    if ip.is_link_local:
        return "a link-local address (this range holds cloud metadata services)"
    if ip.is_private:
        return "a private network address"
    if ip.is_multicast:
        return "a multicast address"
    if ip.is_unspecified or ip.is_reserved:
        return "a reserved address"
    if not ip.is_global:
        return "an address that is not publicly routable"

    return None


def literal_address(host):
    """The host as an IP address when it is one (also bracketed IPv6), else None."""

    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def check_url(url, policy=Policy()):
    """
    Checks a URL without any network access. Returns (scheme, host, port,
    path_with_query). Raises UnsafeDestination with the reason.
    """

    text = str(url or "").strip()

    if not text or len(text) > MAX_URL_LENGTH:
        raise UnsafeDestination(f"The URL must be 1 to {MAX_URL_LENGTH} characters.")

    if any(ord(char) < 33 or ord(char) == 127 for char in text):
        raise UnsafeDestination("The URL must not contain spaces or control characters.")

    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise UnsafeDestination("The URL is not valid.") from None

    scheme = parts.scheme.lower()

    if scheme not in ("https", "http"):
        raise UnsafeDestination("The URL must start with https://.")

    if scheme == "http" and not policy.allow_http:
        raise UnsafeDestination("Webhook destinations must use HTTPS.")

    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UnsafeDestination("The URL must not contain a user name or password.")

    host = (parts.hostname or "").rstrip(".").lower()

    if not host:
        raise UnsafeDestination("The URL has no host name.")

    if parts.fragment:
        raise UnsafeDestination("The URL must not contain a fragment (#...).")

    port = port or (443 if scheme == "https" else 80)

    if not (port == 443 or 1024 <= port <= 65535 or (port == 80 and policy.allow_http)):
        raise UnsafeDestination("The port must be 443 or between 1024 and 65535.")

    if not policy.allow_private:

        if host in BLOCKED_NAMES or host.endswith(BLOCKED_SUFFIXES):
            raise UnsafeDestination("The host name points at the server itself or an internal network.")

        literal = literal_address(host)

        if literal is not None:
            problem = address_problem(str(literal))
            if problem:
                raise UnsafeDestination(f"The destination is {problem}.")

        elif "." not in host:
            raise UnsafeDestination("The host name must be a fully qualified public name (with a domain).")

        elif host.replace(".", "").isdigit() or host.startswith("0x"):
            # Forms such as 2130706433 or 0x7f.1 that some resolvers read as addresses
            raise UnsafeDestination("The host name looks like an encoded IP address.")

    path = parts.path or "/"

    if parts.query:
        path = f"{path}?{parts.query}"

    return scheme, host, port, path


def resolve_destination(url, policy=Policy(), resolver=socket.getaddrinfo):
    """
    Checks the URL, resolves its host and checks every address. Returns the
    Destination to connect to (the first acceptable address). Raises
    UnsafeDestination (refused) or OSError (the name could not be resolved).
    """

    scheme, host, port, path = check_url(url, policy)
    literal = literal_address(host)

    if literal is not None:
        addresses = [str(literal)]
    else:
        infos = resolver(host, port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(info[4][0] for info in infos))

    if not addresses:
        raise OSError("The host name has no address.")

    if not policy.allow_private:
        for address in addresses:
            problem = address_problem(address.split("%")[0])
            if problem:
                raise UnsafeDestination(f"The host name resolves to {problem}.")

    return Destination(scheme, host, port, path, addresses[0])
