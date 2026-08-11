from __future__ import annotations

import json
import sys
from pathlib import Path

from app.config import Settings
from app.main import create_app


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: export_openapi.py OUTPUT")
    output = Path(sys.argv[1]).resolve()
    schema = create_app(Settings(data_dir=output.parent)).openapi()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
