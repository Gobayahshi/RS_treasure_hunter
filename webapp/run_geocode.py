"""좌표가 없는 판매점을 카카오 주소검색으로 변환한다.

Cursor를 꺼도 이 스크립트는 독립 프로세스로 계속 실행할 수 있다.
진행 상황은 geocode_progress.log 를 보면 된다.
"""

import sys
from datetime import datetime
from pathlib import Path

from db import db_session, init_db
from geocode import geocode_missing_stores

LOG_PATH = Path(__file__).with_name("geocode_progress.log")


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            stream.write(data)
            stream.flush()

    def flush(self):
        for stream in self.streams:
            stream.flush()


if __name__ == "__main__":
    log_file = open(LOG_PATH, "a", encoding="utf-8")
    sys.stdout = Tee(sys.stdout, log_file)
    sys.stderr = Tee(sys.stderr, log_file)
    print(f"\n=== geocode start {datetime.now().isoformat(timespec='seconds')} ===")
    init_db()
    with db_session() as conn:
        result = geocode_missing_stores(conn)
    print(
        "DONE",
        result["provider"],
        "filled",
        result["filled"],
        "failed",
        result["failed_count"],
        "attempted",
        result["attempted"],
    )
    for line in result.get("failed") or []:
        print("FAIL", line)
    print(f"=== geocode end {datetime.now().isoformat(timespec='seconds')} ===")
    log_file.close()
