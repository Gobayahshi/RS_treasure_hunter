"""사내 Sales_info 형식 업로드, 파일에 없는 사원 삭제, 테스트 계정, 대소문자 무시 로그인."""

import uuid
from datetime import datetime
from io import BytesIO

from openpyxl import Workbook


def admin_auth(token):
    return {"X-Admin-Token": token}


def rep_auth(token):
    return {"X-Rep-Token": token}


def build_sales_info(rows, sheet_title="Sheet1"):
    """사내 Sales_info 파일과 같은 컬럼 구성."""
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title
    ws.append(["파트너코드", "파트너명", "근무처코드", "직원명", "LOGIN-ID", "전화번호뒤4자리"])
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def upload(client, admin_token, data, filename="Sales_info.xlsx", remove_missing=False):
    form = {"files": (BytesIO(data), filename)}
    if remove_missing:
        form["remove_missing_reps"] = "1"
    return client.post(
        "/api/import/excel", data=form, headers=admin_auth(admin_token), content_type="multipart/form-data"
    )


def make_dealer(code="D13469", name="코넥"):
    from db import db_session

    with db_session() as conn:
        row = conn.execute("SELECT id FROM dealers WHERE dealer_code = ?", (code,)).fetchone()
        if row:
            return row["id"]
        dealer_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO dealers (id, dealer_code, name, created_at) VALUES (?,?,?,?)",
            (dealer_id, code, name, datetime.utcnow().isoformat()),
        )
        return dealer_id


def test_sales_info_sheet_creates_reps(client, server, admin_token):
    from db import db_session

    make_dealer("D13469", "코넥")
    data = build_sales_info(
        [
            ["D13469", "코넥", "D134690211", "김선휘", "D13469968", "6068"],
            ["D13469", "코넥", "D134690203", "선용택", "D1346923643", 3210],
        ]
    )
    res = upload(client, admin_token, data)
    assert res.status_code == 200, res.get_json()
    summary = res.get_json()["reps"]
    # 시드 DB 에 이미 있는 고유ID 는 수정으로 잡힌다.
    assert summary["created"] + summary["updated"] == 2, summary
    assert summary["skipped"] == 0, summary

    with db_session() as conn:
        rep = conn.execute("SELECT * FROM reps WHERE employee_code = 'D13469968'").fetchone()
        assert rep["name"] == "김선휘"
        assert rep["phone_last4"] == "6068"
        assert rep["must_change_password"] == 1  # 초기 비밀번호 = 고유ID
        other = conn.execute("SELECT * FROM reps WHERE employee_code = 'D1346923643'").fetchone()
        assert other["phone_last4"] == "3210"  # 숫자로 들어와도 4자리로 저장

    # 올린 사람은 고유ID를 비밀번호로 첫 로그인할 수 있다
    login = client.post(
        "/api/auth/login", json={"employee_code": "D13469968", "password": "D13469968"}
    )
    assert login.status_code == 200
    assert login.get_json()["must_change_password"] is True


def test_login_and_reset_ignore_id_letter_case(client, server, admin_token):
    make_dealer("D15051", "프리스비")
    upload(
        client,
        admin_token,
        build_sales_info([["D15051", "프리스비", "D150510001", "김철수", "D15051ABC", "1234"]]),
    )

    # 소문자로 입력해도 로그인된다
    lower = client.post(
        "/api/auth/login", json={"employee_code": "d15051abc", "password": "D15051ABC"}
    )
    assert lower.status_code == 200

    # 비밀번호도 대소문자를 가린다(비밀번호 자체는 구분한다)
    wrong_pw = client.post(
        "/api/auth/login", json={"employee_code": "d15051abc", "password": "d15051abc"}
    )
    assert wrong_pw.status_code == 401

    # 재설정도 소문자 ID로 된다
    reset = client.post(
        "/api/auth/reset-password",
        json={"employee_code": "d15051abc", "phone": "1234", "new_password": "newpass1"},
    )
    assert reset.status_code == 200
    assert client.post(
        "/api/auth/login", json={"employee_code": "D15051abc", "password": "newpass1"}
    ).status_code == 200

    # 재고 화면 로그인도 마찬가지
    inv = client.post("/api/inventory/login", json={"username": "d15051abc", "password": "newpass1"})
    assert inv.status_code == 200


def test_phone_only_sheet_fills_existing_reps(client, server, admin_token, fixtures):
    """고유ID + 전화번호 뒤 4자리만 있는 파일도 받는다."""
    from db import db_session

    wb = Workbook()
    ws = wb.active
    ws.append(["LOGIN-ID", "전화번호뒤4자리"])
    ws.append([fixtures["employee_code"], "9876"])
    buf = BytesIO()
    wb.save(buf)

    res = upload(client, admin_token, buf.getvalue(), filename="전화번호.xlsx")
    assert res.status_code == 200
    assert res.get_json()["reps"]["updated"] == 1
    with db_session() as conn:
        row = conn.execute(
            "SELECT phone_last4 FROM reps WHERE id = ?", (fixtures["rep_id"],)
        ).fetchone()
    assert row["phone_last4"] == "9876"


def test_remove_missing_reps_deletes_people_and_their_records(client, server, admin_token, fixtures, rep_token):
    """파일에 없는 사원은 방문·포인트 기록까지 지운다."""
    from conftest import complete_visit
    from db import db_session

    complete_visit(client, rep_token, fixtures["store_id"])
    with db_session() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM point_ledger WHERE rep_id = ?", (fixtures["rep_id"],)
        ).fetchone()["c"] == 1

    make_dealer("D14746", "유원")
    data = build_sales_info([["D14746", "유원", "D147460001", "남은사람", "D14746KEEP", "1111"]])
    res = upload(client, admin_token, data, remove_missing=True)
    assert res.status_code == 200
    summary = res.get_json()["reps"]
    assert summary["created"] + summary["updated"] == 1, summary
    assert summary["removed"] >= 1, summary

    with db_session() as conn:
        assert conn.execute("SELECT 1 FROM reps WHERE id = ?", (fixtures["rep_id"],)).fetchone() is None
        assert conn.execute(
            "SELECT COUNT(*) c FROM point_ledger WHERE rep_id = ?", (fixtures["rep_id"],)
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM visit_sessions WHERE rep_id = ?", (fixtures["rep_id"],)
        ).fetchone()["c"] == 0
        # 파일에 있던 사람은 남는다
        assert conn.execute("SELECT 1 FROM reps WHERE employee_code = 'D14746KEEP'").fetchone()


def test_upload_without_flag_keeps_everyone(client, server, admin_token, fixtures):
    from db import db_session

    make_dealer("D13952", "참스타")
    upload(client, admin_token, build_sales_info([["D13952", "참스타", "x", "새사람", "D13952NEW", "2222"]]))
    with db_session() as conn:
        assert conn.execute("SELECT 1 FROM reps WHERE id = ?", (fixtures["rep_id"],)).fetchone()


# ---------------------------------------------------------------------------
# 테스트 계정
# ---------------------------------------------------------------------------


def test_create_and_delete_test_accounts(client, server, admin_token, fixtures):
    from db import db_session

    with db_session() as conn:
        dealer_code = conn.execute(
            "SELECT dealer_code FROM dealers WHERE id = ?", (fixtures["dealer_id"],)
        ).fetchone()["dealer_code"]

    res = client.post("/api/admin/test-accounts", headers=admin_auth(admin_token))
    assert res.status_code == 201
    body = res.get_json()
    assert body["created"] >= 2

    code1 = f"{dealer_code.upper()}TEST_1"
    login = client.post("/api/auth/login", json={"employee_code": code1.lower(), "password": code1})
    assert login.status_code == 200
    # 시연용이라 비밀번호 변경을 강요하지 않는다
    assert login.get_json()["must_change_password"] is False
    assert client.get(
        "/api/treasures/nearby?lat=37.5&lng=127.0", headers=rep_auth(login.get_json()["token"])
    ).status_code == 200
    # 재고 화면도 같은 ID 로 쓴다
    assert client.post(
        "/api/inventory/login", json={"username": code1, "password": code1}
    ).status_code == 200
    # 전화번호 뒤 4자리 0000 으로 재설정도 시험할 수 있다
    assert client.post(
        "/api/auth/reset-password",
        json={"employee_code": code1, "phone": "0000", "new_password": "demo1234"},
    ).status_code == 200

    # 다시 눌러도 중복 생성되지 않는다
    again = client.post("/api/admin/test-accounts", headers=admin_auth(admin_token)).get_json()
    assert again["created"] == 0
    assert again["already"] >= 2

    removed = client.delete("/api/admin/test-accounts", headers=admin_auth(admin_token)).get_json()
    assert removed["removed"] >= 2
    with db_session() as conn:
        assert conn.execute(
            "SELECT 1 FROM reps WHERE UPPER(employee_code) = ?", (code1,)
        ).fetchone() is None
        # 진짜 사원은 남아 있다
        assert conn.execute("SELECT 1 FROM reps WHERE id = ?", (fixtures["rep_id"],)).fetchone()


def test_test_accounts_require_super(client, admin_token, rep_token):
    assert client.post("/api/admin/test-accounts").status_code == 401
    assert client.post("/api/admin/test-accounts", headers=admin_auth(rep_token)).status_code == 401
