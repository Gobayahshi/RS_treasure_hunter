"""Render/gunicorn 엔트리포인트.

루트 파일 이름이 app.py 이면 gunicorn app:app 이 자기 자신을 불러
배포가 실패한다. 이 파일은 그 충돌을 피한다.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEBAPP = ROOT / "webapp"

sys.path.insert(0, str(WEBAPP))
os.chdir(WEBAPP)

from app import app  # noqa: E402
