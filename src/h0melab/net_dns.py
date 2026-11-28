"""The panel's DNS choice, applied inside this process the moment it is saved.

The container runs unprivileged, so /etc/resolv.conf cannot be rewritten.
Instead every lookup made by this process is routed through the chosen public
servers:

* Python networking (niquests' system resolver, aiohttp, urllib, ...) goes
  through a wrapped ``socket.getaddrinfo`` that asks the servers over plain DNS.
* curl_cffi resolves inside libcurl, where Python cannot reach, so libcurl is
  given the same provider's DNS-over-HTTPS endpoint for every request.

Local names (IP literals, single-label Docker service names, ``.local``, ...)
and anything the public servers cannot answer keep using the system resolver,
so Jellyfin, Plex or other containers on the same network stay reachable.

Separate processes (the captcha Chromium, ffmpeg) keep the system resolver.
"""

import ipaddress
import os
import socket
import threading

from .logger import get_logger

DNS_KEY = "H0MELAB_DNS_SERVERS"
DEFAULT_DNS_MODE = "system"

# mode -> (plain DNS servers, DoH endpoint for libcurl)
DNS_PROVIDERS = {
    "system": ((), None),
    "cloudflare": (("1.1.1.1", "1.0.0.1"), "https://1.1.1.1/dns-query"),
    "google": (("8.8.8.8", "8.8.4.4"), "https://8.8.8.8/dns-query"),
}
DNS_MODES = tuple(DNS_PROVIDERS)

# Seconds one server may take before the next one, then the system, is asked.
_QUERY_TIMEOUT = 2.0
_LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localdomain", ".arpa")

logger = get_logger(__name__)

_system_getaddrinfo = socket.getaddrinfo
_resolvers = {}
_resolvers_lock = threading.Lock()
_installed = False


def dns_mode():
    """The saved DNS choice; anything unknown falls back to the system resolver."""
    mode = os.getenv(DNS_KEY, DEFAULT_DNS_MODE).strip().lower()
    return mode if mode in DNS_PROVIDERS else DEFAULT_DNS_MODE


def dns_servers(mode=None):
    return DNS_PROVIDERS[mode or dns_mode()][0]


def doh_url(mode=None):
    return DNS_PROVIDERS[mode or dns_mode()][1]


def _host_text(host):
    if isinstance(host, bytes):
        try:
            host = host.decode("idna")
        except UnicodeError:
            return None
    if not isinstance(host, str):
        return None
    return host.strip().rstrip(".").lower()


def _is_public_name(host):
    """Only real internet names go to the public servers."""
    if not host or "." not in host or host == "localhost":
        return False
    if host.endswith(_LOCAL_SUFFIXES):
        return False
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return True
    return False


def _server_resolver(server):
    """One cached UDP resolver per server; they are thread-safe and cache answers."""
    with _resolvers_lock:
        resolver = _resolvers.get(server)
        if resolver is None or not resolver.is_available():
            from urllib3.contrib.resolver import ResolverDescription

            resolver = ResolverDescription.from_url(
                f"dou://{server}?timeout={_QUERY_TIMEOUT}"
            ).new()
            _resolvers[server] = resolver
        return resolver


def _drop_resolver(server):
    with _resolvers_lock:
        resolver = _resolvers.pop(server, None)
    if resolver is not None:
        try:
            resolver.close()
        except Exception:
            pass


def reset_dns_cache():
    """Forget every cached resolver and answer, e.g. after the choice changed."""
    with _resolvers_lock:
        servers = list(_resolvers)
    for server in servers:
        _drop_resolver(server)


def _normalise(results, family, flags, host):
    """Return addresses in the shape the socket module promises.

    The resolver can attach extra record data in the canonname slot, and AAAA
    answers are of no use in containers without IPv6, so IPv4 comes first.
    """
    canonname = host if flags & socket.AI_CANONNAME else ""
    seen = set()
    entries = []
    for entry in results:
        if len(entry) != 5:
            continue
        fam, kind, proto, _canon, sockaddr = entry
        if fam not in (socket.AF_INET, socket.AF_INET6):
            continue
        if family not in (0, socket.AF_UNSPEC) and fam != family:
            continue
        key = (fam, kind, proto, sockaddr)
        if key in seen:
            continue
        seen.add(key)
        entries.append((fam, kind, proto, canonname, sockaddr))
    entries.sort(key=lambda item: item[0] != socket.AF_INET)
    return entries


def _public_getaddrinfo(servers, host, port, family, type, proto, flags):
    for server in servers:
        try:
            results = _server_resolver(server).getaddrinfo(
                host, port, family, type, proto, flags
            )
        except socket.gaierror:
            # A clear "no such name" answer; the next server will say the same
            # most of the time, the system resolver may know a private name.
            continue
        except Exception as exc:
            logger.debug("DNS lookup for %s via %s failed (%s)", host, server, exc)
            _drop_resolver(server)
            continue
        entries = _normalise(results, family, flags, host)
        if entries:
            return entries
    return None


def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """socket.getaddrinfo, answered by the chosen public servers when set."""
    servers = dns_servers()
    name = _host_text(host)
    if servers and _is_public_name(name):
        entries = _public_getaddrinfo(servers, name, port, family, type, proto, flags)
        if entries:
            return entries
        logger.debug("Public DNS had no answer for %s, using the system resolver", name)
    return _system_getaddrinfo(host, port, family, type, proto, flags)


def _install_curl_cffi():
    """Give every curl_cffi request the provider's DoH endpoint."""
    try:
        from curl_cffi import CurlOpt
        from curl_cffi.requests import session as curl_session
    except ImportError:
        return
    original = getattr(curl_session, "set_curl_options", None)
    if original is None or getattr(original, "_h0melab_dns", False):
        if original is None:
            logger.warning("curl_cffi changed; the DNS setting does not reach it")
        return

    def set_curl_options(curl, *args, **kwargs):
        result = original(curl, *args, **kwargs)
        url = doh_url()
        if url and not kwargs.get("doh_url"):
            curl.setopt(CurlOpt.DOH_URL, url.encode())
        return result

    set_curl_options._h0melab_dns = True
    curl_session.set_curl_options = set_curl_options


def install():
    """Route this process' lookups through the DNS setting. Safe to call twice."""
    global _installed
    if _installed:
        return
    socket.getaddrinfo = getaddrinfo
    _install_curl_cffi()
    _installed = True
    mode = dns_mode()
    if mode != DEFAULT_DNS_MODE:
        logger.info("DNS: using %s (%s)", mode, ", ".join(dns_servers(mode)))


def apply_dns_mode(previous=None):
    """Called after the setting was saved; new lookups use it right away."""
    reset_dns_cache()
    mode = dns_mode()
    if previous != mode:
        servers = ", ".join(dns_servers(mode)) or "system resolver"
        logger.info("DNS switched to %s (%s)", mode, servers)
