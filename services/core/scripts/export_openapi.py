"""Export the FastAPI OpenAPI document for client generation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from zara.api.app import create_app
from zara.config import get_settings


def main() -> None:
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("openapi.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    document = create_app(get_settings()).openapi()
    destination.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {destination}")


if __name__ == "__main__":
    main()
