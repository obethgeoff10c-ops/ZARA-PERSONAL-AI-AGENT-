"""Run the API with uvicorn using validated settings."""

from __future__ import annotations

import uvicorn

from zara.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "zara.api.app:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
        access_log=False,
        proxy_headers=settings.trust_proxy_headers,
    )


if __name__ == "__main__":
    main()
