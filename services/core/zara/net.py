"""Small network helpers."""

from __future__ import annotations

import ipaddress


def sanitize_ip(value: str | None) -> str | None:
    """Return a canonical IP literal, or ``None`` for non-IP client identifiers.

    Starlette's test client and some proxies report non-IP hosts; storing those
    in an ``inet`` column would fail the whole request for an audit nicety.
    """
    if not value:
        return None
    candidate = value.strip()
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None
