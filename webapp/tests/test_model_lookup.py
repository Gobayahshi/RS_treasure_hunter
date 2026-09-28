"""모델 조회(펫네임→대표모델) 업로드와, 재고 챗봇의 펫네임 인식 회귀 테스트."""

from io import BytesIO

from openpyxl import Workbook


def admin_auth(token):
    return {"X-Admin-Token": token}


def build_model_lookup_xlsx(rows):
    wb = Workbook()
    ws = wb.active
    ws.append(["모델명", "대표모델", "펫네임"])
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_upload_model_lookup_replaces_existing_rows(client, admin_token):
    data = build_model_lookup_xlsx(
        [
            ["SM-F971N", "SM-F971N", "갤럭시 Z 폴드8"],
            ["SM-F776N", "SM-F776N", "갤럭시 Z 플립8"],
        ]
    )
    res = client.post(
        "/api/admin/model-lookup",
        data={"file": (BytesIO(data), "모델 조회.xlsx")},
        headers=admin_auth(admin_token),
        content_type="multipart/form-data",
    )
    assert res.status_code == 200, res.get_json()
    status = res.get_json()
    assert status["row_count"] == 2

    # 다시 올리면(다른 내용) 이전 행이 완전히 지워지고 새 파일 내용만 남는다.
    data2 = build_model_lookup_xlsx([["SM-S931N", "SM-S931N", "갤럭시 S25"]])
    res2 = client.post(
        "/api/admin/model-lookup",
        data={"file": (BytesIO(data2), "모델 조회.xlsx")},
        headers=admin_auth(admin_token),
        content_type="multipart/form-data",
    )
    assert res2.status_code == 200, res2.get_json()
    assert res2.get_json()["row_count"] == 1

    status_res = client.get("/api/admin/model-lookup", headers=admin_auth(admin_token))
    assert status_res.status_code == 200
    assert status_res.get_json()["row_count"] == 1


def test_resolve_petname_models_requires_unique_match(server, admin_token):
    from db import db_session
    from inventory import resolve_petname_models

    with db_session() as conn:
        conn.execute("DELETE FROM model_lookup")
        conn.executemany(
            "INSERT INTO model_lookup (model_name, canonical_model, petname) VALUES (?, ?, ?)",
            [
                ("SM-F971N", "SM-F971N", "갤럭시 Z 폴드8"),
                ("SM-F976N", "SM-F976N", "갤럭시 Z 폴드8 울트라"),
                ("SM-F776N", "SM-F776N", "갤럭시 Z 플립8"),
            ],
        )
        conn.commit()

    with db_session() as conn:
        # "플립8" 은 "갤럭시 Z 플립8" 하나에만 걸린다.
        assert resolve_petname_models(conn, "플립8 재고 몇 대야") == ["SM-F776N"]
        # "폴드8" 은 폴드8/폴드8 울트라 둘 다에 걸려 임의로 고르지 않는다.
        assert resolve_petname_models(conn, "폴드8 재고 어디 많아") == []
        # 세대 숫자가 없는 말은 다른 세대와 겹치기 쉬워 아예 시도하지 않는다.
        assert resolve_petname_models(conn, "폴드 재고") == []


def test_ask_inventory_resolves_petname_to_model(server, admin_token):
    from db import db_session
    from inventory_chat import ask_inventory

    with db_session() as conn:
        conn.execute("DELETE FROM model_lookup")
        conn.execute(
            "INSERT INTO model_lookup (model_name, canonical_model, petname) VALUES (?, ?, ?)",
            ("SM-F776N", "SM-F776N", "갤럭시 Z 플립8"),
        )
        conn.commit()

    with db_session() as conn:
        result = ask_inventory(conn, "플립8 재고 어디 많아")
        assert result["model"] == "SM-F776N"
