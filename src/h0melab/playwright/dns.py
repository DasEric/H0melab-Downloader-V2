"""Bounded DoH lookups for SerienStream's separate Chromium process.

Only configured source hosts are mapped. Navigation keeps its original URL,
HTTPS hostname and cookie scope; other browser traffic keeps normal DNS.
"""

import os
import threading
import time
from ipaddress import IPv4Address

from niquests import Session

# Literal resolver addresses avoid needing working system DNS to bootstrap DoH.
# Both endpoints support DNS JSON and validate with their IP certificate SANs.
_DOH_ENDPOINTS = (
    ("Cloudflare", "https://1.1.1.1/dns-query"),
    ("Google", "https://8.8.8.8/resolve"),
)
_MAX_CACHE_SECONDS = 300
_cache = {}
_host_locks = {}
_cache_lock = threading.Lock()


def clear_browser_dns_cache(host=None):
    """Expire a failed connection's mapping, or all mappings when requested."""
    with _cache_lock:
        if host is None:
            _cache.clear()
        else:
            _cache.pop(str(host).lower().rstrip("."), None)


def _query_doh(host, endpoint):
    from ..config import CA_CERT_BUNDLE

    with Session(retries=0, disable_http3=True, multiplexed=False) as session:
        # This dedicated client carries no app cookies, netrc credentials or
        # proxy environment. A failed direct lookup leaves browser DNS intact.
        session.trust_env = False
        session.verify = CA_CERT_BUNDLE
        response = session.get(
            endpoint,
            params={"name": host, "type": "A"},
            headers={"Accept": "application/dns-json"},
            timeout=(2, 3),
            allow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict) or payload.get("Status") != 0 or payload.get("TC"):
        raise ValueError("DoH returned no complete successful answer")
    questions = payload.get("Question")
    if not isinstance(questions, list) or not any(
        isinstance(q, dict)
        and q.get("type") == 1
        and str(q.get("name", "")).lower().rstrip(".") == host
        for q in questions
    ):
        raise ValueError("DoH question did not match the requested host")
    answers = payload.get("Answer")
    if not isinstance(answers, list):
        raise TypeError("DoH returned no address record list")
    records = [record for record in answers if isinstance(record, dict)]
    owners = {host}
    cname_ttls = []
    # Follow only CNAMEs rooted in our question, never unrelated answer records.
    for _ in range(len(records)):
        previous = len(owners)
        for record in records:
            owner = str(record.get("name", "")).lower().rstrip(".")
            target = str(record.get("data", "")).lower().rstrip(".")
            if record.get("type") == 5 and owner in owners and target not in owners:
                owners.add(target)
                cname_ttls.append(max(0, int(record.get("TTL", 0))))
        if len(owners) == previous:
            break
    for record in records:
        owner = str(record.get("name", "")).lower().rstrip(".")
        if record.get("type") != 1 or owner not in owners:
            continue
        try:
            address = IPv4Address(str(record.get("data", "")))
        except ValueError:
            continue
        if not address.is_global or address.is_multicast:
            continue
        ttl = min([max(0, int(record.get("TTL", 0))), _MAX_CACHE_SECONDS, *cname_ttls])
        return str(address), ttl
    raise ValueError("DoH returned no usable public IPv4 address")


def _resolve_host(host, logger, check_control):
    with _cache_lock:
        host_lock = _host_locks.setdefault(host, threading.Lock())
    # A second worker may wait on the first lookup, but remains cancellable.
    while not host_lock.acquire(timeout=0.1):
        if check_control is not None:
            check_control()
    try:
        if check_control is not None:
            check_control()
        with _cache_lock:
            cached = _cache.get(host)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        for provider, endpoint in _DOH_ENDPOINTS:
            if check_control is not None:
                check_control()
            try:
                address, ttl = _query_doh(host, endpoint)
            except Exception as exc:
                logger.debug(
                    "SerienStream DoH lookup failed for %s via %s (%s)",
                    host,
                    provider,
                    type(exc).__name__,
                )
                continue
            with _cache_lock:
                _cache[host] = (time.monotonic() + ttl, address)
            return address
        logger.warning(
            "SerienStream DoH lookup failed for %s via Cloudflare and Google; "
            "keeping the browser's normal DNS resolution",
            host,
        )
        return None
    finally:
        host_lock.release()


def browser_dns_args(hosts, check_control=None):
    """Build exact-host Chromium mappings with a five-minute maximum lifetime.

    Failed lookups are not cached. The normal browser resolver remains a
    fallback, including on IPv6-only networks where these IPv4 endpoints fail.
    """
    if os.getenv("H0MELAB_STO_BROWSER_DNS", "1").strip() == "0":
        return []
    from ..config import STO_DOMAINS
    from ..logger import get_logger

    allowed = {host.lower() for host in STO_DOMAINS}
    allowed.update("www." + host for host in tuple(allowed))
    rules = []
    for host in dict.fromkeys(str(host).lower().rstrip(".") for host in hosts):
        if host not in allowed:
            continue
        address = _resolve_host(host, get_logger(__name__), check_control)
        if address is not None:
            rules.append(f"MAP {host} {address}")
    return ["--host-resolver-rules=" + ",".join(rules)] if rules else []
