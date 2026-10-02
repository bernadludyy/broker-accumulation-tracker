from __future__ import annotations

import json
import platform
import tempfile
from pathlib import Path

from . import __version__
from .core import JAKARTA, now_jakarta


def main() -> int:
    with tempfile.TemporaryDirectory() as temp_dir:
        probe = Path(temp_dir) / "write-probe"
        probe.write_text("ok", encoding="utf-8")
        if probe.read_text(encoding="utf-8") != "ok":
            raise RuntimeError("filesystem write probe failed")

    print(json.dumps({
        "project_fabric": __version__,
        "python": platform.python_version(),
        "timezone": str(JAKARTA),
        "jakarta_now": now_jakarta().isoformat(timespec="seconds"),
        "status": "ok",
    }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
