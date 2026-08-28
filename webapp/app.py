import json
import math
import os
import re
import secrets
import threading
import uuid
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, Response, g, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

from confidence import (
    LocationSampleInput,
    PreviousSessionContext,
    RULES_CONFIG,
    evaluate_visit_session,
    haversine_distance_meters,
)
from db import db_session, init_db, start_store_seed_sync
from excel_import import build_stats_xlsx, build_template_xlsx, parse_uploads, upsert_masters
from geocode import geocode_missing_stores
from inventory import (
    inventory_dealer_roster,
    inventory_map_points,
    inventory_model_breakdown,
    inventory_model_catalog,
    inventory_overview,
    parse_inventory_file,
    replace_inventory,
)
from inventory_chat import ask_inventory

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
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024
app.wsgi_app = _PrefixMiddleware(app.wsgi_app, CONTEXT_PATH)
_LOG_DIR = Path("/tmp") if CONTEXT_PATH else Path(__file__).resolve().parent
GEOCODE_LOG_PATH = _LOG_DIR / "geocode_progress.log"
init_db()
start_store_seed_sync()


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


def _static_v(*rels: str) -> str:
    latest = 0
    root = Path(app.static_folder)
    for rel in rels:
        path = root / rel
        if path.exists():
            latest = max(latest, int(path.stat().st_mtime))
    return str(latest or int(datetime.now().timestamp()))


def new_id() -> str:
    return uuid.uuid4().hex


def now_iso() -> str:
    return datetime.utcnow().isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def row_to_dict(row) -> dict:
    return dict(row) if row is not None else None


def mask_person_name(name: str) -> str:
    """영업사원 랭킹용. 성과는 보여 주되 실명은 가린다. 예: 홍길동 → 홍*동, 고바야시 → 고**시"""
    text = (name or "").strip()
    if not text:
        return "익명"
    if len(text) == 1:
        return "*"
    if len(text) == 2:
        return text[0] + "*"
    return text[0] + ("*" * (len(text) - 2)) + text[-1]


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


ADMIN_SESSION_DAYS = 7


def _point_defaults(conn) -> dict[str, int]:
    defaults = {"normal": 10, "rare": 30}
    rows = conn.execute(
        "SELECT key, value FROM app_settings WHERE key IN ('points_normal', 'points_rare')"
    ).fetchall()
    for row in rows:
        try:
            value = int(row["value"])
        except (TypeError, ValueError):
            continue
        if row["key"] == "points_normal":
            defaults["normal"] = value
        elif row["key"] == "points_rare":
            defaults["rare"] = value
    return defaults


def _award_points_for(treasure, defaults: dict[str, int]) -> int:
    if treasure is None:
        return defaults["normal"]
    raw = treasure["points"] if "points" in treasure.keys() else None
    if raw is not None:
        return int(raw)
    return defaults.get(treasure["tier"] or "normal", defaults["normal"])


def _admin_from_token(conn, token: str):
    if not token:
        return None
    row = conn.execute(
        """
        SELECT a.id, a.username, a.dealer_id,
               COALESCE(NULLIF(a.role, ''), 'super') AS role,
               d.name AS dealer_name, d.dealer_code,
               s.created_at AS session_created_at
        FROM admin_sessions s
        JOIN admins a ON a.id = s.admin_id
        LEFT JOIN dealers d ON d.id = a.dealer_id
        WHERE s.token = ?
        """,
        (token,),
    ).fetchone()
    if not row:
        return None
    created = parse_iso(row["session_created_at"])
    if datetime.utcnow() - created > timedelta(days=ADMIN_SESSION_DAYS):
        conn.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "dealer_id": row["dealer_id"] or "",
        "dealer_code": row["dealer_code"] or "",
        "dealer_name": row["dealer_name"] or "",
        "role": "dealer" if (row["role"] == "dealer" or row["dealer_id"]) else "super",
    }


def _is_dealer_user(user: dict | None) -> bool:
    return bool(user) and (user.get("role") == "dealer" or user.get("dealer_id"))


def _admin_auth_error():
    token = (request.headers.get("X-Admin-Token") or "").strip()
    with db_session() as conn:
        admin = _admin_from_token(conn, token)
    if not admin:
        return jsonify({"error": "ADMIN_AUTH_REQUIRED", "message": "관리자 로그인이 필요합니다."}), 401
    g.admin = admin
    return None


def require_admin(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        err = _admin_auth_error()
        if err is not None:
            return err
        if _is_dealer_user(g.admin):
            return jsonify(
                {
                    "error": "ADMIN_ONLY",
                    "message": "대리점 계정은 재고 화면(/inventory)에서 이용하세요.",
                }
            ), 403
        return fn(*args, **kwargs)

    return wrapped


def _inventory_auth_error():
    token = (request.headers.get("X-Admin-Token") or "").strip()
    with db_session() as conn:
        user = _admin_from_token(conn, token)
    if not user:
        return jsonify({"error": "INVENTORY_AUTH_REQUIRED", "message": "대리점 로그인이 필요합니다."}), 401
    g.inventory_user = user
    return None


def require_inventory_user(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        err = _inventory_auth_error()
        if err is not None:
            return err
        return fn(*args, **kwargs)

    return wrapped


def _scoped_dealer_id():
    user = getattr(g, "inventory_user", None) or {}
    if _is_dealer_user(user):
        return user.get("dealer_id") or ""
    return ""


def _create_session(conn, admin_id: str) -> str:
    cutoff = (datetime.utcnow() - timedelta(days=ADMIN_SESSION_DAYS)).isoformat()
    conn.execute("DELETE FROM admin_sessions WHERE created_at < ?", (cutoff,))
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO admin_sessions (token, admin_id, created_at) VALUES (?, ?, ?)",
        (token, admin_id, now_iso()),
    )
    return token


def _user_payload(user: dict, token: str | None = None) -> dict:
    data = {
        "username": user.get("username") or "",
        "role": user.get("role") or "super",
        "dealer_id": user.get("dealer_id") or "",
        "dealer_code": user.get("dealer_code") or "",
        "dealer_name": user.get("dealer_name") or "",
        "can_see_all": not _is_dealer_user(user),
    }
    if token:
        data["token"] = token
    return data


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


@app.route("/inventory")
def inventory_page():
    html_path = Path(app.static_folder) / "inventory.html"
    html = html_path.read_text(encoding="utf-8")
    v = _static_v("css/style.css", "js/inventory-chat.js")
    html = re.sub(r"(css/style\.css)(?:\?v=[^\"']*)?", rf"\1?v={v}", html, count=1)
    html = re.sub(r"(js/inventory-chat\.js)(?:\?v=[^\"']*)?", rf"\1?v={v}", html, count=1)
    resp = Response(_inject_app_base(html), mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
# 관리자 로그인
# ---------------------------------------------------------------------------


@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    body = request.get_json(force=True)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        return jsonify({"error": "username, password required", "message": "아이디와 비밀번호를 입력해주세요."}), 400

    with db_session() as conn:
        admin = conn.execute("SELECT * FROM admins WHERE username = ?", (username,)).fetchone()
        if not admin or not check_password_hash(admin["password_hash"], password):
            return jsonify({"error": "INVALID_ADMIN", "message": "관리자 아이디 또는 비밀번호가 올바르지 않습니다."}), 401
        dealer_id = admin["dealer_id"] if "dealer_id" in admin.keys() else ""
        role = admin["role"] if "role" in admin.keys() else "super"
        if (role or "") == "dealer" or dealer_id:
            return jsonify(
                {
                    "error": "DEALER_USE_INVENTORY",
                    "message": "대리점 계정은 /inventory 재고 화면에서 로그인해주세요.",
                }
            ), 403
        token = _create_session(conn, admin["id"])
        return jsonify({"token": token, "username": admin["username"]})


@app.route("/api/admin/logout", methods=["POST"])
@require_admin
def admin_logout():
    token = (request.headers.get("X-Admin-Token") or "").strip()
    with db_session() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
    return jsonify({"ok": True})


@app.route("/api/admin/me")
@require_admin
def admin_me():
    return jsonify({"username": g.admin["username"]})


@app.route("/api/inventory/login", methods=["POST"])
def inventory_login():
    body = request.get_json(force=True)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        return jsonify({"error": "username, password required", "message": "아이디와 비밀번호를 입력해주세요."}), 400

    with db_session() as conn:
        admin = conn.execute("SELECT * FROM admins WHERE username = ?", (username,)).fetchone()
        if not admin or not check_password_hash(admin["password_hash"], password):
            return jsonify({"error": "INVALID_LOGIN", "message": "아이디 또는 비밀번호가 올바르지 않습니다."}), 401
        token = _create_session(conn, admin["id"])
        user = _admin_from_token(conn, token)
        return jsonify(_user_payload(user or {"username": admin["username"]}, token))


@app.route("/api/inventory/logout", methods=["POST"])
@require_inventory_user
def inventory_logout():
    token = (request.headers.get("X-Admin-Token") or "").strip()
    with db_session() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
    return jsonify({"ok": True})


@app.route("/api/inventory/me")
@require_inventory_user
def inventory_me():
    return jsonify(_user_payload(g.inventory_user))


@app.route("/api/inventory/summary")
@require_inventory_user
def inventory_summary():
    with db_session() as conn:
        scoped = _scoped_dealer_id() or None
        data = inventory_overview(conn, scoped)
        if not scoped:
            roster = inventory_dealer_roster(conn)
            data["dealers"] = roster["dealers"]
            data["dealer_count"] = roster["dealer_count"]
            data["uploaded_count"] = roster["uploaded_count"]
            data["pending_count"] = roster["pending_count"]
        return jsonify(data)


@app.route("/api/inventory/catalog")
@require_inventory_user
def inventory_catalog():
    dealer_id = _scoped_dealer_id() or (request.args.get("dealer_id") or "").strip() or None
    with db_session() as conn:
        return jsonify(inventory_model_catalog(conn, dealer_id))


@app.route("/api/admin/change-password", methods=["POST"])
@require_admin
def admin_change_password():
    body = request.get_json(force=True)
    current_password = body.get("current_password") or ""
    new_password = body.get("new_password") or ""
    if not current_password or not new_password:
        return jsonify({"error": "current_password, new_password required"}), 400
    if len(new_password) < 4:
        return jsonify({"error": "PASSWORD_TOO_SHORT", "message": "새 비밀번호는 4자 이상이어야 합니다."}), 400

    with db_session() as conn:
        admin = conn.execute("SELECT * FROM admins WHERE id = ?", (g.admin["id"],)).fetchone()
        if not admin or not check_password_hash(admin["password_hash"], current_password):
            return jsonify({"error": "INVALID_PASSWORD", "message": "현재 비밀번호가 올바르지 않습니다."}), 401
        conn.execute(
            "UPDATE admins SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), admin["id"]),
        )
        return jsonify({"ok": True, "message": "비밀번호가 변경되었습니다."})


@app.route("/api/admin/settings", methods=["GET", "POST"])
@require_admin
def admin_settings():
    with db_session() as conn:
        if request.method == "GET":
            defaults = _point_defaults(conn)
            return jsonify({"points_normal": defaults["normal"], "points_rare": defaults["rare"]})

        body = request.get_json(force=True) or {}
        try:
            points_normal = int(body.get("points_normal"))
            points_rare = int(body.get("points_rare"))
        except (TypeError, ValueError):
            return jsonify({"error": "points_normal, points_rare required"}), 400
        if points_normal < 1 or points_rare < 1 or points_normal > 100000 or points_rare > 100000:
            return jsonify({"error": "INVALID_POINTS", "message": "포인트는 1~100000 사이여야 합니다."}), 400
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('points_normal', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(points_normal),),
        )
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('points_rare', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(points_rare),),
        )
        return jsonify({"points_normal": points_normal, "points_rare": points_rare})


@app.route("/api/admin/treasures")
@require_admin
def list_admin_treasures():
    with db_session() as conn:
        defaults = _point_defaults(conn)
        rows = conn.execute(
            """
            SELECT t.*, s.name AS store_name, s.address AS store_address,
                   s.store_code, s.lat AS store_lat, s.lng AS store_lng
            FROM treasures t
            JOIN stores s ON s.id = t.store_id
            WHERE s.store_code LIKE 'ADMIN-%'
            ORDER BY t.active_date DESC
            LIMIT 200
            """
        ).fetchall()
        items = []
        for row in rows:
            item = row_to_dict(row)
            item["award_points"] = _award_points_for(row, defaults)
            items.append(item)
        return jsonify(items)


@app.route("/api/admin/treasures/plant", methods=["POST"])
@require_admin
def plant_treasure():
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip() or "관리자 지정 보물"
    try:
        lat = float(body["lat"])
        lng = float(body["lng"])
        points = int(body["points"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "lat, lng, points required", "message": "위치와 포인트를 입력해주세요."}), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify({"error": "INVALID_COORDS", "message": "위도/경도가 올바르지 않습니다."}), 400
    if points < 1 or points > 100000:
        return jsonify({"error": "INVALID_POINTS", "message": "포인트는 1~100000 사이여야 합니다."}), 400

    store_id = new_id()
    treasure_id = new_id()
    store_code = f"ADMIN-{treasure_id[:10].upper()}"
    address = f"ADMIN/{treasure_id}"
    tier = "rare" if points >= 30 else "normal"

    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO stores (
                id, dealer_id, store_code, name, address, detail_address, lat, lng, created_at
            ) VALUES (?, NULL, ?, ?, ?, '', ?, ?, ?)
            """,
            (store_id, store_code, name, address, lat, lng, now_iso()),
        )
        conn.execute(
            """
            INSERT INTO treasures (id, store_id, tier, lat, lng, active_date, points)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (treasure_id, store_id, tier, lat, lng, now_iso(), points),
        )
        row = conn.execute(
            """
            SELECT t.*, s.name AS store_name, s.address AS store_address, s.store_code
            FROM treasures t JOIN stores s ON s.id = t.store_id
            WHERE t.id = ?
            """,
            (treasure_id,),
        ).fetchone()
        data = row_to_dict(row)
        data["award_points"] = points
        return jsonify(data), 201


@app.route("/api/admin/treasures/<treasure_id>", methods=["PATCH"])
@require_admin
def update_treasure_points(treasure_id):
    body = request.get_json(force=True) or {}
    try:
        points = int(body["points"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "points required"}), 400
    if points < 1 or points > 100000:
        return jsonify({"error": "INVALID_POINTS", "message": "포인트는 1~100000 사이여야 합니다."}), 400

    with db_session() as conn:
        treasure = conn.execute("SELECT * FROM treasures WHERE id = ?", (treasure_id,)).fetchone()
        if not treasure:
            return jsonify({"error": "TREASURE_NOT_FOUND"}), 404
        if treasure["claimed_at"]:
            return jsonify({"error": "ALREADY_CLAIMED", "message": "이미 획득된 보물은 포인트를 바꿀 수 없습니다."}), 409
        tier = "rare" if points >= 30 else "normal"
        conn.execute(
            "UPDATE treasures SET points = ?, tier = ? WHERE id = ?",
            (points, tier, treasure_id),
        )
        updated = conn.execute("SELECT * FROM treasures WHERE id = ?", (treasure_id,)).fetchone()
        data = row_to_dict(updated)
        data["award_points"] = points
        return jsonify(data)


@app.route("/api/admin/treasures/<treasure_id>", methods=["DELETE"])
@require_admin
def delete_admin_treasure(treasure_id):
    with db_session() as conn:
        row = conn.execute(
            """
            SELECT t.*, s.store_code, s.id AS store_id
            FROM treasures t
            JOIN stores s ON s.id = t.store_id
            WHERE t.id = ?
            """,
            (treasure_id,),
        ).fetchone()
        if not row:
            return jsonify({"error": "TREASURE_NOT_FOUND"}), 404
        if not (row["store_code"] or "").startswith("ADMIN-"):
            return jsonify({"error": "NOT_ADMIN_TREASURE", "message": "관리자가 심은 보물만 회수할 수 있습니다."}), 403
        if row["claimed_at"]:
            return jsonify({"error": "ALREADY_CLAIMED", "message": "이미 획득된 보물은 회수할 수 없습니다."}), 409
        conn.execute("DELETE FROM treasures WHERE id = ?", (treasure_id,))
        conn.execute("DELETE FROM stores WHERE id = ?", (row["store_id"],))
        return jsonify({"ok": True})


@app.route("/api/admin/stats.xlsx")
@require_admin
def download_admin_stats():
    with db_session() as conn:
        data = build_stats_xlsx(conn)
    stamp = datetime.utcnow().strftime("%Y%m%d")
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=RS_Treasure_stats_{stamp}.xlsx"},
    )


# ---------------------------------------------------------------------------
# 대리점 / 사원
# ---------------------------------------------------------------------------


@app.route("/api/dealers")
@require_admin
def list_dealers():
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM dealers ORDER BY name").fetchall()
        return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/reps")
@require_admin
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
@require_admin
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
@require_admin
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
@require_admin
def create_store():
    body = request.get_json(force=True)
    try:
        name = body["name"].strip()
        address = body["address"].strip()
        lat = float(body["lat"])
        lng = float(body["lng"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "name, address, lat, lng required"}), 400
    store_code = (body.get("store_code") or "").strip()
    detail_address = (body.get("detail_address") or "").strip()
    if not store_code:
        return jsonify({"error": "store_code required", "message": "판매점코드가 필요합니다."}), 400
    dealer_code = (body.get("dealer_code") or "").strip()

    with db_session() as conn:
        existing = conn.execute("SELECT id FROM stores WHERE store_code = ?", (store_code,)).fetchone()
        if existing:
            return jsonify({"error": "STORE_CODE_EXISTS", "message": "이미 있는 판매점코드입니다."}), 409

        dealer_id = None
        if dealer_code:
            dealer = conn.execute("SELECT * FROM dealers WHERE dealer_code = ?", (dealer_code,)).fetchone()
            if not dealer:
                return jsonify({"error": "DEALER_NOT_FOUND"}), 404
            dealer_id = dealer["id"]

        store_id = new_id()
        conn.execute(
            """
            INSERT INTO stores (
                id, dealer_id, store_code, name, address, detail_address, lat, lng, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (store_id, dealer_id, store_code, name, address, detail_address, lat, lng, now_iso()),
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
        address = d.pop("store_address")
        d.pop("store_code", None)
        d.pop("store_detail_address", None)
        store_count = d.pop("store_count", None) or 1
        d["store"] = {
            "id": d["store_id"],
            "name": d.pop("store_name"),
            "address": address,
            "lat": d.pop("store_lat"),
            "lng": d.pop("store_lng"),
            "store_count": int(store_count),
        }
        result.append(d)
    return result


def _attach_award_points(conn, items: list[dict]) -> list[dict]:
    defaults = _point_defaults(conn)
    for item in items:
        explicit = item.get("points")
        if explicit is not None:
            item["award_points"] = int(explicit)
        else:
            item["award_points"] = defaults.get(item.get("tier") or "normal", defaults["normal"])
    return items


TREASURE_STORE_SELECT = """
            SELECT t.*, s.name as store_name, s.address as store_address,
                   s.lat as store_lat, s.lng as store_lng,
                   (SELECT COUNT(*) FROM stores sx WHERE sx.address = s.address) as store_count
            FROM treasures t
            JOIN stores s ON s.id = t.store_id
"""


def _one_treasure_per_address(items: list[dict]) -> list[dict]:
    """보물찾기는 기본주소가 같으면 한 곳이다. 판매점코드는 쓰지 않는다."""
    by_address: dict[str, dict] = {}
    for item in items:
        address = (item.get("store") or {}).get("address") or ""
        if address in by_address:
            continue
        by_address[address] = item
    return list(by_address.values())


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
            TREASURE_STORE_SELECT
            + """
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
    items = _one_treasure_per_address(items)
    with db_session() as conn:
        _attach_award_points(conn, items)
    return jsonify({"total_in_radius": len(items), "items": items[:limit]})


@app.route("/api/treasures/active")
@require_admin
def active_treasures():
    """관리자 확인용. 전체 목록은 크므로 기본 상한을 둔다."""
    limit = min(int(request.args.get("limit", 500) or 500), 2000)
    with db_session() as conn:
        rows = conn.execute(
            TREASURE_STORE_SELECT
            + """
            WHERE t.claimed_at IS NULL
              AND (s.lat != 0 OR s.lng != 0)
            """,
        ).fetchall()
        items = _one_treasure_per_address(_treasure_rows_to_json(rows))
        _attach_award_points(conn, items)
        return jsonify(items[:limit])


@app.route("/api/treasures/spawn", methods=["POST"])
@require_admin
def spawn_treasures():
    """보물이 없는 주소에 새 보물을 스폰한다. 같은 기본주소는 한 곳이다."""
    RARE_THRESHOLD_DAYS = 14
    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT s.address,
                   MIN(s.id) AS id,
                   MAX(s.lat) AS lat,
                   MAX(s.lng) AS lng,
                   MAX(vs.started_at) AS last_visit
            FROM stores s
            LEFT JOIN visit_sessions vs ON vs.store_id = s.id
            WHERE (s.lat != 0 OR s.lng != 0)
              AND s.address NOT IN (
                  SELECT s2.address
                  FROM treasures t
                  JOIN stores s2 ON s2.id = t.store_id
                  WHERE t.claimed_at IS NULL
              )
            GROUP BY s.address
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
            JOIN stores s ON s.id = vs.store_id
            WHERE pl.rep_id = ? AND s.address = ? AND pl.created_at >= ?
            """,
            (session["rep_id"], store["address"], start_of_day),
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
            treasures = conn.execute(
                """
                SELECT t.* FROM treasures t
                JOIN stores s ON s.id = t.store_id
                WHERE s.address = ? AND t.claimed_at IS NULL
                """,
                (store["address"],),
            ).fetchall()
            treasure = treasures[0] if treasures else None

            points = _award_points_for(treasure, _point_defaults(conn))

            ledger_id = new_id()
            conn.execute(
                """
                INSERT INTO point_ledger (id, rep_id, session_id, points, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (ledger_id, session["rep_id"], session_id, points, f"VISIT_VERIFIED:{store['address']}", now_iso()),
            )
            point_ledger_entry = row_to_dict(
                conn.execute("SELECT * FROM point_ledger WHERE id = ?", (ledger_id,)).fetchone()
            )

            if treasures:
                conn.execute(
                    """
                    UPDATE treasures
                    SET claimed_at = ?, claimed_session_id = ?
                    WHERE claimed_at IS NULL AND store_id IN (
                        SELECT id FROM stores WHERE address = ?
                    )
                    """,
                    (now_iso(), session_id, store["address"]),
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
@require_admin
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


@app.route("/api/stats/rankings")
def public_rankings():
    """영업사원 화면용. 대리점/사원 상위 10. 사원 이름은 마스킹한다."""
    me_id = (request.args.get("rep_id") or "").strip()
    with db_session() as conn:
        dealer_rows = conn.execute(
            """
            SELECT d.name as dealer_name, COALESCE(SUM(pl.points), 0) as total_points
            FROM dealers d
            LEFT JOIN reps r ON r.dealer_id = d.id
            LEFT JOIN point_ledger pl ON pl.rep_id = r.id
            GROUP BY d.id
            HAVING total_points > 0
            ORDER BY total_points DESC
            LIMIT 10
            """
        ).fetchall()
        rep_rows = conn.execute(
            """
            SELECT r.id as rep_id, r.name, d.name as dealer_name,
                   COALESCE(SUM(pl.points), 0) as total_points
            FROM reps r
            LEFT JOIN dealers d ON d.id = r.dealer_id
            LEFT JOIN point_ledger pl ON pl.rep_id = r.id
            GROUP BY r.id
            HAVING total_points > 0
            ORDER BY total_points DESC
            LIMIT 10
            """
        ).fetchall()

    dealers = [
        {"rank": i + 1, "name": row["dealer_name"], "total_points": row["total_points"]}
        for i, row in enumerate(dealer_rows)
    ]
    reps = []
    for i, row in enumerate(rep_rows):
        reps.append(
            {
                "rank": i + 1,
                "name_masked": mask_person_name(row["name"]),
                "dealer_name": row["dealer_name"] or "소속 없음",
                "total_points": row["total_points"],
                "is_me": bool(me_id) and row["rep_id"] == me_id,
            }
        )
    return jsonify({"dealers": dealers, "reps": reps})


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
@require_admin
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
@require_admin
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
@require_admin
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


@app.route("/api/inventory/excel", methods=["POST"])
@require_inventory_user
def import_inventory():
    upload = request.files.get("file") or (request.files.getlist("files") or [None])[0]
    if not upload:
        return jsonify({"error": "재고현황 xlsx 파일을 올려주세요."}), 400
    filename = upload.filename or "inventory.xlsx"
    lower = filename.lower()
    if not (lower.endswith(".xlsx") or lower.endswith(".csv")):
        return jsonify({"error": f"{filename}: .xlsx 또는 .csv 만 지원합니다."}), 400
    data = upload.read()
    try:
        parsed = parse_inventory_file(filename, data)
    except Exception as exc:
        return jsonify({"error": f"재고 엑셀을 읽지 못했습니다: {exc}"}), 400
    if not parsed["rows"]:
        return jsonify({"error": "재고현황 파일로 보이지 않습니다. 보유처매장코드/대표상품명 열이 필요합니다."}), 400
    try:
        with db_session() as conn:
            dealer = None
            scoped = _scoped_dealer_id()
            if scoped:
                dealer = conn.execute("SELECT * FROM dealers WHERE id = ?", (scoped,)).fetchone()
                if not dealer:
                    return jsonify({"error": "대리점 정보를 찾지 못했습니다."}), 400
                dealer = dict(dealer)
            summary = replace_inventory(conn, parsed, now_iso(), new_id, dealer=dealer)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(summary), 200


@app.route("/api/inventory/map")
@require_inventory_user
def inventory_map():
    model = (request.args.get("model") or "").strip()
    include_retail = (request.args.get("include_retail") or "").strip() in {"1", "true", "yes"}
    region = (request.args.get("region") or "").strip()
    keyword = (request.args.get("keyword") or "").strip()
    dealer_id = _scoped_dealer_id() or (request.args.get("dealer_id") or "").strip()
    dealer_code = (request.args.get("dealer") or request.args.get("dealer_code") or "").strip()
    aged_only = (request.args.get("aged_only") or "").strip() in {"1", "true", "yes"}
    product_shorts = []
    for raw in request.args.getlist("product_short"):
        product_shorts.extend([p.strip() for p in str(raw).split(",") if p.strip()])
    model_names = []
    for raw in request.args.getlist("model_name"):
        model_names.extend([p.strip() for p in str(raw).split(",") if p.strip()])
    pin_color = (request.args.get("pin_color") or "").strip()
    lat = lng = None
    bbox = None
    radius_km = None
    if request.args.get("radius_km") not in (None, ""):
        try:
            radius_km = float(request.args.get("radius_km"))
        except ValueError:
            return jsonify({"error": "radius_km는 숫자여야 합니다."}), 400
    if request.args.get("lat") not in (None, "") and request.args.get("lng") not in (None, ""):
        try:
            lat = float(request.args.get("lat"))
            lng = float(request.args.get("lng"))
        except ValueError:
            return jsonify({"error": "lat, lng는 숫자여야 합니다."}), 400
    if all(request.args.get(k) not in (None, "") for k in ("south", "west", "north", "east")):
        try:
            bbox = {
                "south": float(request.args.get("south")),
                "west": float(request.args.get("west")),
                "north": float(request.args.get("north")),
                "east": float(request.args.get("east")),
            }
        except ValueError:
            return jsonify({"error": "south, west, north, east는 숫자여야 합니다."}), 400
    with db_session() as conn:
        if not dealer_id and dealer_code:
            dealer = conn.execute(
                "SELECT id FROM dealers WHERE dealer_code = ?", (dealer_code,)
            ).fetchone()
            dealer_id = dealer["id"] if dealer else dealer_code
        data = inventory_map_points(
            conn,
            model,
            include_retail,
            region=region,
            lat=lat,
            lng=lng,
            keyword=keyword,
            dealer_id=dealer_id or None,
            bbox=bbox,
            aged_only=aged_only,
            radius_km=radius_km,
            product_short=product_shorts,
            model_name=model_names,
            pin_color=pin_color,
        )
        if bbox:
            data["area_model_totals"] = inventory_model_breakdown(
                conn,
                dealer_id=dealer_id or None,
                region=region,
                keyword=keyword,
                bbox=bbox,
                limit=80,
            )
        return jsonify(data)


@app.route("/api/inventory/ask", methods=["POST"])
@require_inventory_user
def inventory_ask():
    body = request.get_json(force=True, silent=True) or {}
    text = (body.get("text") or body.get("message") or "").strip()
    if not text:
        return jsonify({"error": "질문을 입력해주세요."}), 400
    lat = lng = None
    if body.get("lat") not in (None, "") and body.get("lng") not in (None, ""):
        try:
            lat = float(body.get("lat"))
            lng = float(body.get("lng"))
        except (TypeError, ValueError):
            return jsonify({"error": "lat, lng는 숫자여야 합니다."}), 400
    bbox = body.get("bbox")
    dealer_id = _scoped_dealer_id() or (body.get("dealer_id") or "").strip() or None
    try:
        with db_session() as conn:
            result = ask_inventory(
                conn,
                text,
                lat=lat,
                lng=lng,
                bbox=bbox,
                dealer_id=dealer_id,
            )
        return jsonify(result)
    except Exception:
        return jsonify({"error": "ask_failed", "message": "질문을 처리하지 못했습니다. 다시 시도해 주세요."}), 500


@app.route("/api/stores/geocode", methods=["POST"])
@require_admin
def geocode_stores():
    """좌표가 비어 있는 판매점을 주소로 다시 변환한다."""
    with db_session() as conn:
        result = geocode_missing_stores(conn)
    return jsonify(result)


@app.route("/api/stores/geocode/status")
@require_admin
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
