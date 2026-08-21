import json
import math
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, Response
from werkzeug.security import check_password_hash, generate_password_hash

from confidence import (
    LocationSampleInput,
    PreviousSessionContext,
    RULES_CONFIG,
    evaluate_visit_session,
    haversine_distance_meters,
    points_for_tier,
)
from db import db_session, init_db
from excel_import import build_template_xlsx, parse_uploads, upsert_masters
from geocode import geocode_missing_stores

# Playground는 CONTEXT_PATH=/rs-treasure 로 붙인다. 로컬은 빈 값.
CONTEXT_PATH = (os.environ.get("CONTEXT_PATH") or "").rstrip("/")


class _PrefixMiddleware:
    """역프록시 context-path 아래에서 Flask가 동작하도록 SCRIPT_NAME을 맞춘다."""

    def __init__(self, wsgi_app, prefix: str):
        self.app = wsgi_app
        self.prefix = prefix or ""

    def __call__(self, environ, start_response):
        if self.prefix:
            environ["SCRIPT_NAME"] = self.prefix
            path = environ.get("PATH_INFO", "")
            if path.startswith(self.prefix + "/") or path == self.prefix:
                environ["PATH_INFO"] = path[len(self.prefix) :] or "/"
        return self.app(environ, start_response)


app = Flask(__name__, static_folder="static", static_url_path="")
app.wsgi_app = _PrefixMiddleware(app.wsgi_app, CONTEXT_PATH)
_LOG_DIR = Path("/tmp") if CONTEXT_PATH else Path(__file__).resolve().parent
GEOCODE_LOG_PATH = _LOG_DIR / "geocode_progress.log"
init_db()


def _inject_app_base(html: str) -> str:
    """정적 HTML에 API/자산용 base path를 심는다."""
    parts = []
    if CONTEXT_PATH:
        parts.append(f'<base href="{CONTEXT_PATH}/">')
    parts.append(f"<script>window.APP_BASE={json.dumps(CONTEXT_PATH)};</script>")
    snippet = "\n".join(parts) + "\n"
    if "</head>" in html:
        return html.replace("</head>", snippet + "</head>", 1)
    return snippet + html


def new_id() -> str:
    return uuid.uuid4().hex


def now_iso() -> str:
    return datetime.utcnow().isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def row_to_dict(row) -> dict:
    return dict(row) if row is not None else None


def public_rep(row) -> dict:
    """API 응답용. 비밀번호 해시는 절대 내려보내지 않는다."""
    data = row_to_dict(row)
    if not data:
        return data
    data.pop("password_hash", None)
    return data


def hash_password(plain: str) -> str:
    return generate_password_hash(plain)


def default_password_for(employee_code: str) -> str:
    """초기 비밀번호는 고유ID와 동일."""
    return employee_code


def _read_geocode_status_from_log() -> dict:
    if not GEOCODE_LOG_PATH.exists():
        return {"log_exists": False}

    text = GEOCODE_LOG_PATH.read_text(encoding="utf-8", errors="ignore")
    progress_matches = re.findall(
        r"geocode progress (\d+)/(\d+) filled=(\d+) failed=(\d+)",
        text,
    )
    done_matches = re.findall(
        r"DONE\s+(\S+)\s+filled\s+(\d+)\s+failed\s+(\d+)\s+attempted\s+(\d+)",
        text,
    )

    status = {
        "log_exists": True,
        "last_progress": None,
        "last_done": None,
        "last_lines": [line for line in text.strip().splitlines()[-8:] if line.strip()],
    }
    if progress_matches:
        attempted, total, filled, failed = progress_matches[-1]
        status["last_progress"] = {
            "attempted": int(attempted),
            "total": int(total),
            "filled": int(filled),
            "failed": int(failed),
        }
    if done_matches:
        provider, filled, failed, attempted = done_matches[-1]
        status["last_done"] = {
            "provider": provider,
            "filled": int(filled),
            "failed": int(failed),
            "attempted": int(attempted),
        }
    return status


# ---------------------------------------------------------------------------
# 정적 페이지
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    html_path = Path(app.static_folder) / "index.html"
    return Response(_inject_app_base(html_path.read_text(encoding="utf-8")), mimetype="text/html")


@app.route("/admin")
def admin():
    html_path = Path(app.static_folder) / "admin.html"
    return Response(_inject_app_base(html_path.read_text(encoding="utf-8")), mimetype="text/html")


# ---------------------------------------------------------------------------
# 헬스체크
# ---------------------------------------------------------------------------


@app.route("/api/health")
def health():
    return jsonify({"ok": True})


def _rep_with_dealer(conn, rep_id: str):
    return conn.execute(
        """
        SELECT r.*, d.dealer_code, d.name as dealer_name
        FROM reps r
        LEFT JOIN dealers d ON d.id = r.dealer_id
        WHERE r.id = ?
        """,
        (rep_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# 로그인: 엑셀로 등록된 고유ID + 비밀번호
# ---------------------------------------------------------------------------


@app.route("/api/auth/login", methods=["POST"])
def login():
    body = request.get_json(force=True)
    employee_code = (body.get("employee_code") or "").strip()
    password = body.get("password") or ""
    if not employee_code:
        return jsonify({"error": "employee_code required"}), 400
    if not password:
        return jsonify({"error": "password required", "message": "비밀번호를 입력해주세요."}), 400

    with db_session() as conn:
        rep = conn.execute(
            """
            SELECT r.*, d.dealer_code, d.name as dealer_name
            FROM reps r
            LEFT JOIN dealers d ON d.id = r.dealer_id
            WHERE r.employee_code = ?
            """,
            (employee_code,),
        ).fetchone()
        if not rep:
            return jsonify({"error": "UNREGISTERED_EMPLOYEE", "message": "등록되지 않은 고유ID입니다. 관리자에게 엑셀 등록을 요청하세요."}), 404

        stored = rep["password_hash"]
        if not stored:
            # 마이그레이션 누락 대비: 즉시 초기 비번(고유ID)으로 채운다.
            stored = hash_password(default_password_for(employee_code))
            conn.execute("UPDATE reps SET password_hash = ? WHERE id = ?", (stored, rep["id"]))

        if not check_password_hash(stored, password):
            return jsonify({"error": "INVALID_PASSWORD", "message": "비밀번호가 올바르지 않습니다."}), 401

        result = public_rep(rep)
        result["using_initial_password"] = check_password_hash(stored, employee_code)
        return jsonify(result)


@app.route("/api/auth/change-password", methods=["POST"])
def change_password():
    body = request.get_json(force=True)
    rep_id = (body.get("rep_id") or "").strip()
    current_password = body.get("current_password") or ""
    new_password = body.get("new_password") or ""

    if not rep_id or not current_password or not new_password:
        return jsonify({"error": "rep_id, current_password, new_password required"}), 400
    if len(new_password) < 4:
        return jsonify({"error": "PASSWORD_TOO_SHORT", "message": "새 비밀번호는 4자 이상이어야 합니다."}), 400
    if new_password == current_password:
        return jsonify({"error": "SAME_PASSWORD", "message": "현재 비밀번호와 다른 값을 입력해주세요."}), 400

    with db_session() as conn:
        rep = conn.execute("SELECT * FROM reps WHERE id = ?", (rep_id,)).fetchone()
        if not rep:
            return jsonify({"error": "REP_NOT_FOUND"}), 404
        stored = rep["password_hash"] or ""
        if not stored or not check_password_hash(stored, current_password):
            return jsonify({"error": "INVALID_PASSWORD", "message": "현재 비밀번호가 올바르지 않습니다."}), 401

        conn.execute(
            "UPDATE reps SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), rep_id),
        )
        return jsonify({"ok": True, "message": "비밀번호가 변경되었습니다."})


# ---------------------------------------------------------------------------
# 대리점 / 사원
# ---------------------------------------------------------------------------


@app.route("/api/dealers")
def list_dealers():
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM dealers ORDER BY name").fetchall()
        return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/reps")
def list_reps():
    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT r.*, d.dealer_code, d.name as dealer_name
            FROM reps r
            LEFT JOIN dealers d ON d.id = r.dealer_id
            ORDER BY r.name
            """
        ).fetchall()
        return jsonify([public_rep(r) for r in rows])


@app.route("/api/reps", methods=["POST"])
def create_rep():
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    employee_code = (body.get("employee_code") or "").strip()
    dealer_code = (body.get("dealer_code") or "").strip()
    if not name or not employee_code:
        return jsonify({"error": "name, employee_code required"}), 400

    with db_session() as conn:
        dealer_id = None
        if dealer_code:
            dealer = conn.execute("SELECT * FROM dealers WHERE dealer_code = ?", (dealer_code,)).fetchone()
            if not dealer:
                return jsonify({"error": "DEALER_NOT_FOUND"}), 404
            dealer_id = dealer["id"]

        existing = conn.execute(
            "SELECT * FROM reps WHERE employee_code = ?", (employee_code,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE reps SET name = ?, dealer_id = COALESCE(?, dealer_id) WHERE id = ?",
                (name, dealer_id, existing["id"]),
            )
            # 비밀번호가 비어 있으면 초기값(고유ID)으로 채운다. 이미 바꾼 비번은 유지.
            if not existing["password_hash"]:
                conn.execute(
                    "UPDATE reps SET password_hash = ? WHERE id = ?",
                    (hash_password(default_password_for(employee_code)), existing["id"]),
                )
            rep = _rep_with_dealer(conn, existing["id"])
        else:
            rep_id = new_id()
            conn.execute(
                """
                INSERT INTO reps (id, dealer_id, name, employee_code, password_hash, device_id, created_at)
                VALUES (?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    rep_id,
                    dealer_id,
                    name,
                    employee_code,
                    hash_password(default_password_for(employee_code)),
                    now_iso(),
                ),
            )
            rep = _rep_with_dealer(conn, rep_id)
        return jsonify(public_rep(rep)), 201


@app.route("/api/reps/<rep_id>")
def get_rep(rep_id):
    with db_session() as conn:
        rep = _rep_with_dealer(conn, rep_id)
        if not rep:
            return jsonify({"error": "REP_NOT_FOUND"}), 404
        return jsonify(public_rep(rep))


# ---------------------------------------------------------------------------
# 판매점(Store) - 관리자용
# ---------------------------------------------------------------------------


@app.route("/api/stores", methods=["GET"])
def list_stores():
    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT s.*, d.dealer_code, d.name as dealer_name
            FROM stores s
            LEFT JOIN dealers d ON d.id = s.dealer_id
            ORDER BY s.created_at DESC
            """
        ).fetchall()
        return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/stores", methods=["POST"])
def create_store():
    body = request.get_json(force=True)
    try:
        name = body["name"].strip()
        address = body["address"].strip()
        lat = float(body["lat"])
        lng = float(body["lng"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "name, address, lat, lng required"}), 400
    dealer_code = (body.get("dealer_code") or "").strip()

    with db_session() as conn:
        dealer_id = None
        if dealer_code:
            dealer = conn.execute("SELECT * FROM dealers WHERE dealer_code = ?", (dealer_code,)).fetchone()
            if not dealer:
                return jsonify({"error": "DEALER_NOT_FOUND"}), 404
            dealer_id = dealer["id"]

        store_id = new_id()
        conn.execute(
            "INSERT INTO stores (id, dealer_id, name, address, lat, lng, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (store_id, dealer_id, name, address, lat, lng, now_iso()),
        )
        store = conn.execute(
            """
            SELECT s.*, d.dealer_code, d.name as dealer_name
            FROM stores s LEFT JOIN dealers d ON d.id = s.dealer_id WHERE s.id = ?
            """,
            (store_id,),
        ).fetchone()
        return jsonify(row_to_dict(store)), 201


# ---------------------------------------------------------------------------
# 보물(Treasure)
# ---------------------------------------------------------------------------


def _treasure_rows_to_json(rows) -> list[dict]:
    result = []
    for r in rows:
        d = row_to_dict(r)
        d["store"] = {
            "id": d["store_id"],
            "name": d.pop("store_name"),
            "address": d.pop("store_address"),
            "lat": d.pop("store_lat"),
            "lng": d.pop("store_lng"),
        }
        result.append(d)
    return result


@app.route("/api/treasures/nearby")
def nearby_treasures():
    """현재 위치 주변 보물만 반환한다.

    판매점이 수천 곳이라 전체를 내려보내면 휴대폰에서 느려진다.
    먼저 위경도 사각형(bounding box)으로 후보를 줄이고, 실제 거리로 정렬해 상위 N개만 준다.
    """
    try:
        lat = float(request.args.get("lat", ""))
        lng = float(request.args.get("lng", ""))
    except ValueError:
        return jsonify({"error": "lat, lng required"}), 400

    radius_km = min(float(request.args.get("radius_km", 5) or 5), 50)
    limit = min(int(request.args.get("limit", 30) or 30), 100)

    lat_delta = radius_km / 111.0
    lng_delta = radius_km / max(1.0, 111.0 * math.cos(math.radians(lat)))

    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT t.*, s.name as store_name, s.address as store_address,
                   s.lat as store_lat, s.lng as store_lng
            FROM treasures t
            JOIN stores s ON s.id = t.store_id
            WHERE t.claimed_at IS NULL
              AND s.lat BETWEEN ? AND ?
              AND s.lng BETWEEN ? AND ?
            """,
            (lat - lat_delta, lat + lat_delta, lng - lng_delta, lng + lng_delta),
        ).fetchall()

    items = _treasure_rows_to_json(rows)
    for item in items:
        item["distance_meters"] = haversine_distance_meters(
            lat, lng, item["store"]["lat"], item["store"]["lng"]
        )
    items = [i for i in items if i["distance_meters"] <= radius_km * 1000]
    items.sort(key=lambda i: i["distance_meters"])
    return jsonify({"total_in_radius": len(items), "items": items[:limit]})


@app.route("/api/treasures/active")
def active_treasures():
    """관리자 확인용. 전체 목록은 크므로 기본 상한을 둔다."""
    limit = min(int(request.args.get("limit", 500) or 500), 2000)
    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT t.*, s.name as store_name, s.address as store_address,
                   s.lat as store_lat, s.lng as store_lng
            FROM treasures t
            JOIN stores s ON s.id = t.store_id
            WHERE t.claimed_at IS NULL
              AND (s.lat != 0 OR s.lng != 0)
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return jsonify(_treasure_rows_to_json(rows))


@app.route("/api/treasures/spawn", methods=["POST"])
def spawn_treasures():
    """보물이 없는 매장에 새 보물을 스폰한다. 오래 미방문한 매장일수록 rare 등급."""
    RARE_THRESHOLD_DAYS = 14
    with db_session() as conn:
        # 매장 수천 건을 한 번에 처리하므로 매장별 재조회(N+1) 대신 마지막 방문일을 한 번에 집계한다.
        rows = conn.execute(
            """
            SELECT s.id, s.lat, s.lng, MAX(vs.started_at) AS last_visit
            FROM stores s
            LEFT JOIN visit_sessions vs ON vs.store_id = s.id
            WHERE s.id NOT IN (SELECT store_id FROM treasures WHERE claimed_at IS NULL)
              AND (s.lat != 0 OR s.lng != 0)
            GROUP BY s.id
            """
        ).fetchall()

        now = datetime.utcnow()
        created_at = now_iso()
        payload = []
        for row in rows:
            if row["last_visit"]:
                days_since_visit = (now - parse_iso(row["last_visit"])).total_seconds() / 86400
            else:
                days_since_visit = float("inf")
            tier = "rare" if days_since_visit >= RARE_THRESHOLD_DAYS else "normal"
            payload.append((new_id(), row["id"], tier, row["lat"], row["lng"], created_at))

        conn.executemany(
            "INSERT INTO treasures (id, store_id, tier, lat, lng, active_date) VALUES (?, ?, ?, ?, ?, ?)",
            payload,
        )

        return jsonify({"spawned": len(payload)}), 201


# ---------------------------------------------------------------------------
# 방문 인증 세션(VisitSession) - 핵심 GPS 부정행위 방지 로직
# ---------------------------------------------------------------------------


@app.route("/api/visit-sessions", methods=["POST"])
def start_visit_session():
    body = request.get_json(force=True)
    rep_id = body.get("rep_id")
    store_id = body.get("store_id")
    device_id = body.get("device_id")
    if not rep_id or not store_id:
        return jsonify({"error": "rep_id, store_id required"}), 400

    with db_session() as conn:
        store = conn.execute("SELECT * FROM stores WHERE id = ?", (store_id,)).fetchone()
        if not store:
            return jsonify({"error": "STORE_NOT_FOUND"}), 404

        rep = conn.execute("SELECT * FROM reps WHERE id = ?", (rep_id,)).fetchone()
        if not rep:
            return jsonify({"error": "REP_NOT_FOUND"}), 404

        # R8 준비: 최초 세션 시 디바이스를 계정에 고정(binding)한다.
        if rep["device_id"] is None and device_id:
            conn.execute("UPDATE reps SET device_id = ? WHERE id = ?", (device_id, rep_id))

        session_id = new_id()
        conn.execute(
            """
            INSERT INTO visit_sessions (id, rep_id, store_id, device_id, started_at, status, flag_reasons)
            VALUES (?, ?, ?, ?, ?, 'in_progress', '[]')
            """,
            (session_id, rep_id, store_id, device_id, now_iso()),
        )
        session = conn.execute("SELECT * FROM visit_sessions WHERE id = ?", (session_id,)).fetchone()
        return jsonify(row_to_dict(session)), 201


@app.route("/api/visit-sessions/<session_id>/samples", methods=["POST"])
def add_location_sample(session_id):
    body = request.get_json(force=True)
    try:
        lat = float(body["lat"])
        lng = float(body["lng"])
        accuracy = float(body["accuracy"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "lat, lng, accuracy required"}), 400
    is_mock = bool(body.get("is_mock", False))

    with db_session() as conn:
        session = conn.execute(
            "SELECT * FROM visit_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not session:
            return jsonify({"error": "SESSION_NOT_FOUND"}), 404
        if session["status"] != "in_progress":
            return jsonify({"error": "SESSION_ALREADY_FINALIZED"}), 409

        sample_id = new_id()
        conn.execute(
            """
            INSERT INTO location_samples (id, session_id, lat, lng, accuracy, is_mock, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (sample_id, session_id, lat, lng, accuracy, 1 if is_mock else 0, now_iso()),
        )
        sample = conn.execute(
            "SELECT * FROM location_samples WHERE id = ?", (sample_id,)
        ).fetchone()
        return jsonify(row_to_dict(sample)), 201


@app.route("/api/visit-sessions/<session_id>/complete", methods=["POST"])
def complete_visit_session(session_id):
    with db_session() as conn:
        session = conn.execute(
            "SELECT * FROM visit_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not session:
            return jsonify({"error": "SESSION_NOT_FOUND"}), 404
        if session["status"] != "in_progress":
            return jsonify({"error": "SESSION_ALREADY_FINALIZED"}), 409

        store = conn.execute("SELECT * FROM stores WHERE id = ?", (session["store_id"],)).fetchone()
        rep = conn.execute("SELECT * FROM reps WHERE id = ?", (session["rep_id"],)).fetchone()
        sample_rows = conn.execute(
            "SELECT * FROM location_samples WHERE session_id = ?", (session_id,)
        ).fetchall()

        ended_at = datetime.utcnow()
        started_at = parse_iso(session["started_at"])

        samples = [
            LocationSampleInput(
                lat=r["lat"],
                lng=r["lng"],
                accuracy=r["accuracy"],
                is_mock=bool(r["is_mock"]),
                captured_at=parse_iso(r["captured_at"]),
            )
            for r in sample_rows
        ]

        prev_row = conn.execute(
            """
            SELECT vs.*, s.lat as store_lat, s.lng as store_lng FROM visit_sessions vs
            JOIN stores s ON s.id = vs.store_id
            WHERE vs.rep_id = ? AND vs.id != ? AND vs.status IN ('auto_approved', 'pending_review')
                  AND vs.ended_at IS NOT NULL
            ORDER BY vs.ended_at DESC LIMIT 1
            """,
            (session["rep_id"], session_id),
        ).fetchone()
        previous_session = (
            PreviousSessionContext(
                store_id=prev_row["store_id"],
                lat=prev_row["store_lat"],
                lng=prev_row["store_lng"],
                ended_at=parse_iso(prev_row["ended_at"]),
            )
            if prev_row
            else None
        )

        start_of_day = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        claimed_today = conn.execute(
            """
            SELECT COUNT(*) as cnt FROM point_ledger pl
            JOIN visit_sessions vs ON vs.id = pl.session_id
            WHERE pl.rep_id = ? AND vs.store_id = ? AND pl.created_at >= ?
            """,
            (session["rep_id"], session["store_id"], start_of_day),
        ).fetchone()["cnt"]

        device_mismatch = bool(
            session["device_id"] and rep["device_id"] and rep["device_id"] != session["device_id"]
        )

        evaluation = evaluate_visit_session(
            store_lat=store["lat"],
            store_lng=store["lng"],
            samples=samples,
            started_at=started_at,
            ended_at=ended_at,
            previous_session=previous_session,
            already_claimed_today=claimed_today > 0,
            device_mismatch=device_mismatch,
        )

        conn.execute(
            """
            UPDATE visit_sessions
            SET ended_at = ?, confidence_score = ?, status = ?, flag_reasons = ?
            WHERE id = ?
            """,
            (ended_at.isoformat(), evaluation.score, evaluation.status, json.dumps(evaluation.reasons), session_id),
        )

        point_ledger_entry = None
        claimed_treasure = None

        if evaluation.status == "auto_approved" and evaluation.points_eligible:
            treasure = conn.execute(
                "SELECT * FROM treasures WHERE store_id = ? AND claimed_at IS NULL LIMIT 1",
                (session["store_id"],),
            ).fetchone()

            points = points_for_tier(treasure["tier"]) if treasure else points_for_tier("normal")

            ledger_id = new_id()
            conn.execute(
                """
                INSERT INTO point_ledger (id, rep_id, session_id, points, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (ledger_id, session["rep_id"], session_id, points, f"VISIT_VERIFIED:{session['store_id']}", now_iso()),
            )
            point_ledger_entry = row_to_dict(
                conn.execute("SELECT * FROM point_ledger WHERE id = ?", (ledger_id,)).fetchone()
            )

            if treasure:
                conn.execute(
                    "UPDATE treasures SET claimed_at = ?, claimed_session_id = ? WHERE id = ?",
                    (now_iso(), session_id, treasure["id"]),
                )
                claimed_treasure = row_to_dict(
                    conn.execute("SELECT * FROM treasures WHERE id = ?", (treasure["id"],)).fetchone()
                )

        updated_session = row_to_dict(
            conn.execute("SELECT * FROM visit_sessions WHERE id = ?", (session_id,)).fetchone()
        )

        return jsonify(
            {
                "session": updated_session,
                "evaluation": {
                    "score": evaluation.score,
                    "status": evaluation.status,
                    "reasons": evaluation.reasons,
                    "points_eligible": evaluation.points_eligible,
                },
                "point_ledger_entry": point_ledger_entry,
                "claimed_treasure": claimed_treasure,
                "rules_config": RULES_CONFIG,
            }
        )


# ---------------------------------------------------------------------------
# 포인트 / 랭킹
# ---------------------------------------------------------------------------


@app.route("/api/points/<rep_id>")
def get_points(rep_id):
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM point_ledger WHERE rep_id = ? ORDER BY created_at DESC", (rep_id,)
        ).fetchall()
        ledgers = [row_to_dict(r) for r in rows]
        total = sum(l["points"] for l in ledgers)
        return jsonify({"total": total, "ledgers": ledgers})


@app.route("/api/points")
def leaderboard():
    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT r.id as rep_id, r.name, r.employee_code, COALESCE(SUM(pl.points), 0) as total_points
            FROM reps r
            LEFT JOIN point_ledger pl ON pl.rep_id = r.id
            GROUP BY r.id
            ORDER BY total_points DESC
            """
        ).fetchall()
        return jsonify([row_to_dict(r) for r in rows])


# ---------------------------------------------------------------------------
# 리워드
# ---------------------------------------------------------------------------


@app.route("/api/rewards", methods=["POST"])
def request_reward():
    body = request.get_json(force=True)
    rep_id = body.get("rep_id")
    reward_type = body.get("type")
    try:
        point_cost = int(body["point_cost"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "rep_id, type, point_cost required"}), 400
    if not rep_id or not reward_type or point_cost <= 0:
        return jsonify({"error": "rep_id, type, point_cost required"}), 400

    with db_session() as conn:
        total = conn.execute(
            "SELECT COALESCE(SUM(points), 0) as total FROM point_ledger WHERE rep_id = ?", (rep_id,)
        ).fetchone()["total"]
        if total < point_cost:
            return jsonify({"error": "INSUFFICIENT_POINTS", "total_points": total}), 400

        reward_id = new_id()
        conn.execute(
            "INSERT INTO rewards (id, rep_id, type, point_cost, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
            (reward_id, rep_id, reward_type, point_cost, now_iso()),
        )
        reward = conn.execute("SELECT * FROM rewards WHERE id = ?", (reward_id,)).fetchone()
        return jsonify(row_to_dict(reward)), 201


@app.route("/api/rewards/<rep_id>")
def list_rewards(rep_id):
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM rewards WHERE rep_id = ? ORDER BY created_at DESC", (rep_id,)
        ).fetchall()
        return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/rewards/<reward_id>/issue", methods=["POST"])
def issue_reward(reward_id):
    with db_session() as conn:
        conn.execute(
            "UPDATE rewards SET status = 'issued', issued_at = ? WHERE id = ?", (now_iso(), reward_id)
        )
        reward = conn.execute("SELECT * FROM rewards WHERE id = ?", (reward_id,)).fetchone()
        if not reward:
            return jsonify({"error": "REWARD_NOT_FOUND"}), 404
        return jsonify(row_to_dict(reward))


# ---------------------------------------------------------------------------
# 엑셀 마스터 업로드
# ---------------------------------------------------------------------------


@app.route("/api/import/template")
def download_import_template():
    data = build_template_xlsx()
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=RS_Treasure_master_template.xlsx"},
    )


def _geocode_in_background() -> None:
    with db_session() as conn:
        geocode_missing_stores(conn)


@app.route("/api/import/excel", methods=["POST"])
def import_excel():
    uploads = request.files.getlist("files")
    if not uploads:
        single = request.files.get("file")
        if single:
            uploads = [single]
    if not uploads:
        return jsonify({"error": "xlsx 파일을 1개 이상 올려주세요."}), 400

    blobs: list[tuple[str, bytes]] = []
    for f in uploads:
        filename = f.filename or "upload.xlsx"
        if not filename.lower().endswith(".xlsx"):
            return jsonify({"error": f"{filename}: .xlsx 만 지원합니다."}), 400
        blobs.append((filename, f.read()))

    try:
        buckets = parse_uploads(blobs)
    except Exception as exc:
        return jsonify({"error": f"엑셀을 읽지 못했습니다: {exc}"}), 400

    with db_session() as conn:
        summary = upsert_masters(conn, buckets, now_iso(), new_id)
        missing = conn.execute(
            "SELECT COUNT(*) AS cnt FROM stores WHERE lat = 0 AND lng = 0"
        ).fetchone()["cnt"]

    # 수천 건은 요청 안에서 돌리면 타임아웃 나므로, 소수만 즉시 변환하고 대량은 백그라운드로 돌린다.
    if missing <= 30:
        with db_session() as conn:
            summary["geocode"] = geocode_missing_stores(conn)
    else:
        threading.Thread(target=_geocode_in_background, daemon=True).start()
        summary["geocode"] = {
            "provider": "background",
            "attempted": missing,
            "filled": 0,
            "failed": [],
            "failed_count": 0,
            "note": f"좌표 없는 매장 {missing}곳은 백그라운드에서 변환합니다.",
        }
    return jsonify(summary), 200


@app.route("/api/stores/geocode", methods=["POST"])
def geocode_stores():
    """좌표가 비어 있는 판매점을 주소로 다시 변환한다."""
    with db_session() as conn:
        result = geocode_missing_stores(conn)
    return jsonify(result)


@app.route("/api/stores/geocode/status")
def geocode_status():
    with db_session() as conn:
        counts = conn.execute(
            """
            SELECT
                COUNT(*) AS total_stores,
                SUM(CASE WHEN lat != 0 OR lng != 0 THEN 1 ELSE 0 END) AS geocoded_stores,
                SUM(CASE WHEN lat = 0 AND lng = 0 THEN 1 ELSE 0 END) AS missing_stores
            FROM stores
            """
        ).fetchone()

    total_stores = counts["total_stores"] or 0
    geocoded_stores = counts["geocoded_stores"] or 0
    missing_stores = counts["missing_stores"] or 0
    percent = round((geocoded_stores / total_stores) * 100, 1) if total_stores else 0

    log_status = _read_geocode_status_from_log()
    return jsonify(
        {
            "total_stores": total_stores,
            "geocoded_stores": geocoded_stores,
            "missing_stores": missing_stores,
            "percent": percent,
            "is_complete": total_stores > 0 and missing_stores == 0,
            "log": log_status,
        }
    )


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "8080"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
