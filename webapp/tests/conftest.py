"""테스트 공통 설정.

운영/로컬 DB를 건드리지 않도록 DB_PATH를 임시 파일로 돌린 뒤에 app을 불러온다.
"""

import os
import sys
import tempfile
import uuid
from datetime import datetime

import pytest

WEBAPP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WEBAPP_DIR)


@pytest.fixture(scope="session")
def server():
    tmp_dir = tempfile.mkdtemp(prefix="rs-treasure-test-")
    tmp_db = os.path.join(tmp_dir, "test.db")
    os.environ["DB_PATH"] = tmp_db
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_INITIAL_PASSWORD"] = "admin"
    os.chdir(WEBAPP_DIR)
    import app as server_module

    # db.py 의 DB_PATH 는 모듈을 맨 처음 import 할 때 딱 한 번만 계산된다. 어떤 테스트 파일이든
    # 최상단(모듈 레벨)에서 app/db 를 먼저 import 해버리면 위에서 정한 임시 경로가 씹히고,
    # 그 뒤로는 이 프로세스에서 도는 테스트 전부가 실제 로컬 운영 DB(webapp/rs_treasure.db)에
    # 쓰게 된다 — 조용히 실패해서 알아차리기 어렵다. 여기서 바로 확인해 크게 터뜨린다.
    # (실제로 2026-09-23에 test_password.py 의 최상단 `import app` 때문에 한동안 이렇게 샜다.)
    import db as db_module

    assert os.path.abspath(db_module.DB_PATH) == os.path.abspath(tmp_db), (
        "테스트가 임시 DB가 아니라 실제 DB_PATH를 보고 있습니다: "
        f"{db_module.DB_PATH!r}. 어떤 tests/*.py 파일이 모듈 최상단에서 "
        "'import app' / 'from db import ...' 를 하고 있는지 확인하세요 "
        "(server/client/fixtures 같은 fixture 안에서만 import 해야 합니다)."
    )
    return server_module


@pytest.fixture()
def client(server):
    return server.app.test_client()


@pytest.fixture()
def fixtures(server):
    """대리점 1곳, 영업사원 2명, 판매점 1곳을 만든다."""
    from db import db_session

    now = datetime.utcnow().isoformat()
    data = {
        "dealer_id": uuid.uuid4().hex,
        "rep_id": uuid.uuid4().hex,
        "other_rep_id": uuid.uuid4().hex,
        "store_id": uuid.uuid4().hex,
        "employee_code": uuid.uuid4().hex[:7],
        "other_employee_code": uuid.uuid4().hex[:7],
        "store_code": f"PT{uuid.uuid4().hex[:4].upper()}",
        "password": "pw1234",
        "lat": 37.5,
        "lng": 127.0,
    }
    with db_session() as conn:
        conn.execute(
            "INSERT INTO dealers (id, dealer_code, name, created_at) VALUES (?,?,?,?)",
            (data["dealer_id"], f"D{uuid.uuid4().hex[:5]}", "테스트대리점", now),
        )
        for key, code, name in (
            ("rep_id", "employee_code", "홍길동"),
            ("other_rep_id", "other_employee_code", "김철수"),
        ):
            conn.execute(
                "INSERT INTO reps (id, dealer_id, name, employee_code, password_hash, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (
                    data[key],
                    data["dealer_id"],
                    name,
                    data[code],
                    server.hash_password(data["password"]),
                    now,
                ),
            )
        conn.execute(
            "INSERT INTO stores (id, dealer_id, store_code, name, address, lat, lng, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                data["store_id"],
                data["dealer_id"],
                data["store_code"],
                "테스트판매점",
                f"서울시 테스트구 {uuid.uuid4().hex[:4]}",
                data["lat"],
                data["lng"],
                now,
            ),
        )
    return data


@pytest.fixture()
def rep_token(client, fixtures):
    res = client.post(
        "/api/auth/login",
        json={"employee_code": fixtures["employee_code"], "password": fixtures["password"]},
    )
    return res.get_json()["token"]


@pytest.fixture()
def admin_token(client):
    res = client.post("/api/admin/login", json={"username": "admin", "password": "admin"})
    return res.get_json()["token"]


def complete_visit(client, token, store_id, accuracy=10.0, lat=37.5, lng=127.0, samples=3):
    """방문 세션을 만들고 체류시간을 채운 뒤 완료한다."""
    from db import db_session

    headers = {"X-Rep-Token": token}
    session_id = client.post(
        "/api/visit-sessions",
        json={"store_id": store_id, "device_id": "test-device"},
        headers=headers,
    ).get_json()["id"]
    for _ in range(samples):
        client.post(
            f"/api/visit-sessions/{session_id}/samples",
            json={"lat": lat, "lng": lng, "accuracy": accuracy},
            headers=headers,
        )
    with db_session() as conn:
        conn.execute(
            "UPDATE visit_sessions SET started_at = datetime('now', '-20 seconds') WHERE id = ?",
            (session_id,),
        )
    result = client.post(f"/api/visit-sessions/{session_id}/complete", headers=headers).get_json()
    return session_id, result
