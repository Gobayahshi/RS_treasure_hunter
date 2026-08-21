"""Playground/로컬 공통 엔트리포인트.

저장소 루트에서 webapp 패키지를 실행한다.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEBAPP = ROOT / "webapp"

# webapp 모듈(db, geocode 등)을 import 할 수 있게 한다.
sys.path.insert(0, str(WEBAPP))
os.chdir(WEBAPP)

from app import app  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
