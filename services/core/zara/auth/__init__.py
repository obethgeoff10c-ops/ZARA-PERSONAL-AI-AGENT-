"""Authentication and authorization primitives.

The managed IdP is the source of truth for identity (D-05); ZARA stores no
passwords. ``local`` is a development-only HS256 provider so Compose and the
test suite can exercise the exact same bearer-token path as production.
"""

from zara.auth.principal import Principal

__all__ = ["Principal"]
