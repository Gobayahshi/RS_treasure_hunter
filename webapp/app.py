import json
import math
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, Response, g, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

from confidence import (
    KST_OFFSET,
    LocationSampleInput,
    PreviousSessionContext,
    RULES_CONFIG,
    evaluate_visit_session,
    haversine_distance_meters,
)
from db import db_session, init_db, start_store_seed_sync
from excel_import import (
    build_stats_xlsx,
    build_template_xlsx,
    normalize_phone,
    normalize_store_code,
    parse_uploads,
    upsert_masters,
)
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


def kst_now() -> datetime:
    """DB에는 UTC로 저장하지만, 날짜/시간 판단은 한국 시간으로 한다."""
    return datetime.utcnow() + KST_OFFSET


def kst_day_start_utc_iso() -> str:
    """오늘(한국 시간) 0시를 UTC ISO로. created_at 비교용."""
    start_kst = kst_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return (start_kst - KST_OFFSET).isoformat()


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


def mask_phone(phone: str) -> str:
    """010-****-5678 형태로만 보여준다. 전체 번호는 화면에 내리지 않는다."""
    digits = normalize_phone(phone)
    if len(digits) < 7:
        return ""
    return f"{digits[:3]}-****-{digits[-4:]}"


def public_rep(row) -> dict:
    """API 응답용. 비밀번호 해시와 전화번호 원본은 절대 내려보내지 않는다."""
    data = row_to_dict(row)
    if not data:
        return data
    data.pop("password_hash", None)
    phone = data.pop("phone", None)
    data["has_phone"] = bool(normalize_phone(phone))
    data["phone_masked"] = mask_phone(phone)
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


def _rep_point_balance(conn, rep_id: str) -> dict:
    """적립 포인트에서 신청·지급된 리워드만큼 뺀 잔액.

    잔액을 따로 저장하지 않고 항상 원장에서 계산한다. 취소(cancelled)된 리워드는 제외한다.
    """
    earned = conn.execute(
        "SELECT COALESCE(SUM(points), 0) AS total FROM point_ledger WHERE rep_id = ?", (rep_id,)
    ).fetchone()["total"]
    spent = conn.execute(
        """
        SELECT COALESCE(SUM(point_cost), 0) AS total FROM rewards
        WHERE rep_id = ? AND status IN ('pending', 'issued')
        """,
        (rep_id,),
    ).fetchone()["total"]
    return {"earned": earned, "spent": spent, "balance": earned - spent}


def _grant_visit_points(conn, session_id: str, rep_id: str, store) -> tuple[dict | None, dict | None]:
    """방문 세션에 포인트를 적립하고 같은 주소의 보물을 회수 처리한다.

    자동 승인과 관리자 수동 승인이 같은 규칙을 쓰도록 한 곳에 모았다.
    point_ledger.session_id 가 UNIQUE 라서 같은 세션에 두 번 적립되지 않는다.
    """
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
        (ledger_id, rep_id, session_id, points, f"VISIT_VERIFIED:{store['address']}", now_iso()),
    )
    point_ledger_entry = row_to_dict(
        conn.execute("SELECT * FROM point_ledger WHERE id = ?", (ledger_id,)).fetchone()
    )

    claimed_treasure = None
    if treasure is not None:
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
    return point_ledger_entry, claimed_treasure


# ---------------------------------------------------------------------------
# 사용자 계층 (.cursor/rules/user-hierarchy.mdc)
#   SKT 총괄  admins.role = 'super'  전국 · 수정 · 계정/권한 부여
#   SKT 직원  admins.role = 'staff'  전국 · 조회 (예외: 대리점 관리자/직원 구분은 바꿀 수 있음)
#   대리점    reps (사원 고유ID)     소속 대리점만 · 조회 + 재고 업로드
#             reps.dealer_role = 'manager' | 'staff' (권한은 같고 구분만 한다)
# ---------------------------------------------------------------------------

SKT_ROLES = {"super", "staff"}
DEALER_ROLES = {"manager", "staff"}

# 초기 비밀번호를 쓰는 동안에도 열어 두는 API. 비밀번호를 바꾸려면 필요하다.
PASSWORD_GATE_EXEMPT = {
    "rep_me",
    "rep_logout",
    "change_password",
    "inventory_me",
    "inventory_logout",
    "admin_me",
    "admin_logout",
    "admin_change_password",
}


def _password_gate(must_change: bool):
    """초기 비밀번호를 바꾸기 전에는 다른 기능을 쓰지 못하게 막는다."""
    if not must_change or request.endpoint in PASSWORD_GATE_EXEMPT:
        return None
    return jsonify(
        {
            "error": "PASSWORD_CHANGE_REQUIRED",
            "message": "초기 비밀번호를 사용 중입니다. 새 비밀번호를 정한 뒤 이용해주세요.",
        }
    ), 403


def _admin_from_token(conn, token: str):
    """SKT 계정(총괄/직원) 세션. 역할이 SKT 가 아니면 로그인으로 인정하지 않는다."""
    if not token:
        return None
    row = conn.execute(
        """
        SELECT a.id, a.username, COALESCE(NULLIF(a.role, ''), 'super') AS role,
               COALESCE(a.must_change_password, 0) AS must_change_password,
               s.created_at AS session_created_at
        FROM admin_sessions s
        JOIN admins a ON a.id = s.admin_id
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
    if row["role"] not in SKT_ROLES:
        return None
    return {
        "kind": "skt",
        "id": row["id"],
        "username": row["username"],
        "name": row["username"],
        "role": row["role"],
        "must_change_password": bool(row["must_change_password"]),
        "dealer_id": "",
        "dealer_code": "",
        "dealer_name": "",
    }


def _dealer_user_from_rep(rep: dict | None) -> dict | None:
    """영업사원 = 대리점 직원. 재고 화면에서도 같은 고유ID로 쓴다."""
    if not rep:
        return None
    return {
        "kind": "rep",
        "id": rep["id"],
        "username": rep["employee_code"],
        "name": rep.get("name") or rep["employee_code"],
        "role": "dealer",
        "must_change_password": bool(rep.get("must_change_password")),
        "dealer_role": rep.get("dealer_role") or "staff",
        "dealer_id": rep.get("dealer_id") or "",
        "dealer_code": rep.get("dealer_code") or "",
        "dealer_name": rep.get("dealer_name") or "",
    }


def _request_token() -> str:
    return (
        request.headers.get("X-Admin-Token") or request.headers.get("X-Rep-Token") or ""
    ).strip()


def _is_dealer_user(user: dict | None) -> bool:
    return bool(user) and user.get("role") == "dealer"


def _skt_auth(allowed_roles: set[str]):
    token = (request.headers.get("X-Admin-Token") or "").strip()
    with db_session() as conn:
        admin = _admin_from_token(conn, token)
    if not admin:
        return jsonify({"error": "ADMIN_AUTH_REQUIRED", "message": "관리자 로그인이 필요합니다."}), 401
    if admin["role"] not in allowed_roles:
        return jsonify(
            {"error": "SUPER_ONLY", "message": "SKT 총괄 계정만 할 수 있습니다. (SKT 직원은 조회만 가능)"}
        ), 403
    g.admin = admin
    return _password_gate(admin.get("must_change_password"))


def require_admin(fn):
    """SKT 총괄만. 데이터를 바꾸거나 계정/권한을 주는 작업."""

    @wraps(fn)
    def wrapped(*args, **kwargs):
        err = _skt_auth({"super"})
        if err is not None:
            return err
        return fn(*args, **kwargs)

    return wrapped


def require_skt(fn):
    """SKT 총괄 또는 SKT 직원. 조회 화면."""

    @wraps(fn)
    def wrapped(*args, **kwargs):
        err = _skt_auth(SKT_ROLES)
        if err is not None:
            return err
        return fn(*args, **kwargs)

    return wrapped


def _is_super() -> bool:
    return (getattr(g, "admin", None) or {}).get("role") == "super"


def _inventory_user_from_token(conn, token: str):
    user = _admin_from_token(conn, token)
    if user:
        return user
    rep = _rep_from_token(conn, token)
    if not rep or not rep.get("dealer_id"):
        # 소속 대리점이 없으면 볼 범위가 정해지지 않으므로 재고 화면을 쓰지 못한다.
        return None
    return _dealer_user_from_rep(rep)


def require_inventory_user(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with db_session() as conn:
            user = _inventory_user_from_token(conn, _request_token())
        if not user:
            return jsonify(
                {"error": "INVENTORY_AUTH_REQUIRED", "message": "재고 화면 로그인이 필요합니다."}
            ), 401
        g.inventory_user = user
        blocked = _password_gate(user.get("must_change_password"))
        if blocked is not None:
            return blocked
        return fn(*args, **kwargs)

    return wrapped


def require_inventory_uploader(fn):
    """재고 업로드: 대리점 직원이면 누구나, SKT 는 총괄만 (SKT 직원은 조회 전용)."""

    @wraps(fn)
    @require_inventory_user
    def wrapped(*args, **kwargs):
        user = g.inventory_user
        if user.get("kind") == "skt" and user.get("role") != "super":
            return jsonify(
                {"error": "VIEW_ONLY", "message": "SKT 직원 계정은 재고를 올릴 수 없습니다. (조회 전용)"}
            ), 403
        return fn(*args, **kwargs)

    return wrapped


def _scoped_dealer_id():
    user = getattr(g, "inventory_user", None) or {}
    if _is_dealer_user(user):
        return user.get("dealer_id") or ""
    return ""


# ---------------------------------------------------------------------------
# 영업사원 세션 - 로그인한 본인만 자기 방문/포인트를 다룰 수 있게 한다
# ---------------------------------------------------------------------------

REP_SESSION_DAYS = 30


def _create_rep_session(conn, rep_id: str) -> str:
    cutoff = (datetime.utcnow() - timedelta(days=REP_SESSION_DAYS)).isoformat()
    conn.execute("DELETE FROM rep_sessions WHERE created_at < ?", (cutoff,))
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO rep_sessions (token, rep_id, created_at) VALUES (?, ?, ?)",
        (token, rep_id, now_iso()),
    )
    return token


def _rep_from_token(conn, token: str):
    if not token:
        return None
    row = conn.execute(
        """
        SELECT r.*, d.dealer_code, d.name AS dealer_name, rs.created_at AS session_created_at
        FROM rep_sessions rs
        JOIN reps r ON r.id = rs.rep_id
        LEFT JOIN dealers d ON d.id = r.dealer_id
        WHERE rs.token = ?
        """,
        (token,),
    ).fetchone()
    if not row:
        return None
    if datetime.utcnow() - parse_iso(row["session_created_at"]) > timedelta(days=REP_SESSION_DAYS):
        conn.execute("DELETE FROM rep_sessions WHERE token = ?", (token,))
        return None
    return dict(row)


def require_rep(fn):
    """영업사원 본인 확인. 요청 본문의 rep_id가 아니라 토큰의 주인을 신뢰한다."""

    @wraps(fn)
    def wrapped(*args, **kwargs):
        # 재고 화면은 X-Admin-Token 헤더로 사원 토큰을 보낸다. 토큰은 rep_sessions 에서만 확인한다.
        token = _request_token()
        with db_session() as conn:
            rep = _rep_from_token(conn, token)
        if not rep:
            return jsonify(
                {"error": "REP_AUTH_REQUIRED", "message": "다시 로그인해주세요."}
            ), 401
        g.rep = rep
        blocked = _password_gate(rep.get("must_change_password"))
        if blocked is not None:
            return blocked
        return fn(*args, **kwargs)

    return wrapped


# ---------------------------------------------------------------------------
# 비밀번호 본인 재설정 (고유ID = SWING ID + 전화번호)
# ---------------------------------------------------------------------------

RESET_MAX_FAILURES = 5  # 같은 고유ID 또는 같은 IP 기준
RESET_WINDOW_SECONDS = 15 * 60
_RESET_FAILURES: dict[str, list[float]] = {}
_RESET_LOCK = threading.Lock()


def _reset_attempts_left(keys: list[str]) -> int:
    """키(고유ID, IP)별 남은 시도 횟수. 전화번호를 무작정 넣어보는 걸 막는다."""
    cutoff = time.time() - RESET_WINDOW_SECONDS
    with _RESET_LOCK:
        worst = 0
        for key in keys:
            fails = [t for t in _RESET_FAILURES.get(key, []) if t > cutoff]
            if fails:
                _RESET_FAILURES[key] = fails
            else:
                _RESET_FAILURES.pop(key, None)
            worst = max(worst, len(fails))
        return max(0, RESET_MAX_FAILURES - worst)


def _record_reset_failure(keys: list[str]) -> None:
    now = time.time()
    with _RESET_LOCK:
        for key in keys:
            _RESET_FAILURES.setdefault(key, []).append(now)


def _clear_reset_failures(keys: list[str]) -> None:
    with _RESET_LOCK:
        for key in keys:
            _RESET_FAILURES.pop(key, None)


@app.route("/api/auth/reset-password", methods=["POST"])
def reset_password():
    """고유ID(SWING ID)와 등록된 전화번호가 맞으면 본인이 바로 새 비밀번호를 정한다."""
    body = request.get_json(force=True, silent=True) or {}
    employee_code = (body.get("employee_code") or "").strip()
    phone = normalize_phone(body.get("phone"))
    new_password = body.get("new_password") or ""

    if not employee_code or not phone or not new_password:
        return jsonify(
            {"error": "BAD_INPUT", "message": "고유ID, 전화번호, 새 비밀번호를 모두 입력해주세요."}
        ), 400
    if len(new_password) < 4:
        return jsonify({"error": "PASSWORD_TOO_SHORT", "message": "새 비밀번호는 4자 이상이어야 합니다."}), 400
    if new_password == employee_code:
        return jsonify(
            {"error": "PASSWORD_TOO_SIMPLE", "message": "고유ID와 다른 비밀번호를 정해주세요."}
        ), 400
    if normalize_phone(new_password) == phone:
        return jsonify(
            {"error": "PASSWORD_TOO_SIMPLE", "message": "전화번호와 다른 비밀번호를 정해주세요."}
        ), 400

    keys = [f"code:{employee_code}", f"ip:{request.remote_addr or 'unknown'}"]
    if _reset_attempts_left(keys) <= 0:
        return jsonify(
            {
                "error": "TOO_MANY_ATTEMPTS",
                "message": "시도가 너무 많습니다. 15분 후에 다시 해주세요. 급하면 관리자에게 문의하세요.",
            }
        ), 429

    # 고유ID가 있는지 없는지 알려주지 않는다. 실패 문구는 항상 같다.
    mismatch = jsonify(
        {"error": "RESET_MISMATCH", "message": "고유ID와 전화번호가 등록된 정보와 다릅니다."}
    ), 401

    with db_session() as conn:
        rep = conn.execute("SELECT * FROM reps WHERE employee_code = ?", (employee_code,)).fetchone()
        stored_phone = normalize_phone(rep["phone"]) if rep else ""
        if not rep or not stored_phone or not secrets.compare_digest(stored_phone, phone):
            _record_reset_failure(keys)
            return mismatch

        conn.execute(
            "UPDATE reps SET password_hash = ?, password_reset_at = ? WHERE id = ?",
            (hash_password(new_password), now_iso(), rep["id"]),
        )
        # 비밀번호가 바뀌었으니 이전 로그인은 모두 끊는다.
        conn.execute("DELETE FROM rep_sessions WHERE rep_id = ?", (rep["id"],))

    _clear_reset_failures(keys)
    app.logger.info("password reset by phone: employee_code=%s", employee_code)
    return jsonify({"ok": True, "message": "비밀번호를 바꿨습니다. 새 비밀번호로 로그인해주세요."})


def _current_rep_id() -> str:
    return (getattr(g, "rep", None) or {}).get("id") or ""


_UPLOAD_JOBS: dict[str, dict] = {}
_UPLOAD_LOCK = threading.RLock()


def _normalize_upload_job_id(raw: str) -> str:
    text = re.sub(r"[^0-9a-fA-F]", "", raw or "")
    return text.lower() if len(text) == 32 else ""


def _upload_job_get(job_id: str) -> dict | None:
    if not job_id:
        return None
    with _UPLOAD_LOCK:
        job = _UPLOAD_JOBS.get(job_id)
        return dict(job) if job else None


def _upload_job_set(job_id: str, **fields) -> dict:
    with _UPLOAD_LOCK:
        job = _UPLOAD_JOBS.setdefault(job_id, {"job_id": job_id})
        job.update(fields)
        job["updated_at"] = time.time()
        _prune_upload_jobs()
        return dict(job)


UPLOAD_JOB_TTL_SECONDS = 6 * 60 * 60


def _prune_upload_jobs() -> None:
    """끝난 지 오래된 업로드 기록을 지운다. 워커가 하나라 메모리에만 쌓인다."""
    cutoff = time.time() - UPLOAD_JOB_TTL_SECONDS
    stale = [
        job_id
        for job_id, job in _UPLOAD_JOBS.items()
        if job.get("status") in {"done", "error"} and float(job.get("updated_at") or 0) < cutoff
    ]
    for job_id in stale:
        _UPLOAD_JOBS.pop(job_id, None)


def _upload_job_payload(job: dict) -> dict:
    payload = {
        "job_id": job.get("job_id") or "",
        "status": job.get("status") or "queued",
        "message": job.get("message") or "",
    }
    if job.get("status") == "done" and job.get("summary"):
        payload.update(job["summary"])
        payload["summary"] = job["summary"]
    return payload


def _run_inventory_upload(job_id: str, filename: str, data: bytes, dealer: dict | None) -> None:
    try:
        _upload_job_set(job_id, status="parsing", message="파일을 읽는 중...")
        parsed = parse_inventory_file(filename, data)
        if not parsed.get("rows"):
            _upload_job_set(
                job_id,
                status="error",
                message="재고현황 파일로 보이지 않습니다. 보유처매장코드/대표상품명 열이 필요합니다.",
            )
            return
        _upload_job_set(
            job_id,
            status="saving",
            message=f"{len(parsed['rows']):,}행을 저장하는 중...",
        )
        with db_session() as conn:
            summary = replace_inventory(conn, parsed, now_iso(), new_id, dealer=dealer)
        name = (summary or {}).get("dealer_name") or (dealer or {}).get("name") or ""
        _upload_job_set(
            job_id,
            status="done",
            summary=summary,
            message=f"{name} 재고 현황을 업데이트 했습니다".strip(),
        )
    except ValueError as exc:
        _upload_job_set(job_id, status="error", message=str(exc))
    except Exception as exc:
        _upload_job_set(job_id, status="error", message=f"재고 파일을 처리하지 못했습니다: {exc}"[:240])


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
    is_dealer = _is_dealer_user(user)
    data = {
        "username": user.get("username") or "",
        "name": user.get("name") or user.get("username") or "",
        "role": user.get("role") or "super",
        "dealer_role": user.get("dealer_role") or "",
        "dealer_id": user.get("dealer_id") or "",
        "dealer_code": user.get("dealer_code") or "",
        "dealer_name": user.get("dealer_name") or "",
        "can_see_all": not is_dealer,
        # SKT 직원은 조회 전용이라 업로드 버튼을 숨긴다.
        "can_upload": is_dealer or user.get("role") == "super",
        "can_edit": user.get("role") == "super",
        "must_change_password": bool(user.get("must_change_password")),
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


def _page_response(html_file: str, script: str) -> Response:
    """정적 페이지에 base path와 캐시 무효화 버전을 붙여 돌려준다.

    JS가 API와 짝을 이루므로(로그인 토큰 등) 예전 파일이 캐시에 남으면 안 된다.
    """
    html = (Path(app.static_folder) / html_file).read_text(encoding="utf-8")
    v = _static_v("css/style.css", script)
    html = re.sub(r"(css/style\.css)(?:\?v=[^\"']*)?", rf"\1?v={v}", html, count=1)
    html = re.sub(rf"({re.escape(script)})(?:\?v=[^\"']*)?", rf"\1?v={v}", html, count=1)
    resp = Response(_inject_app_base(html), mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/")
def index():
    return _page_response("index.html", "js/app.js")


@app.route("/admin")
def admin():
    return _page_response("admin.html", "js/admin.js")


@app.route("/inventory")
def inventory_page():
    return _page_response("inventory.html", "js/inventory-chat.js")


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
        using_initial = check_password_hash(stored, employee_code)
        if bool(rep["must_change_password"]) != using_initial:
            # 플래그와 실제 비밀번호가 어긋나면(수동 변경 등) 실제 값에 맞춘다.
            conn.execute(
                "UPDATE reps SET must_change_password = ? WHERE id = ?",
                (1 if using_initial else 0, rep["id"]),
            )
        result["using_initial_password"] = using_initial
        result["must_change_password"] = using_initial
        result["token"] = _create_rep_session(conn, rep["id"])
        return jsonify(result)


@app.route("/api/auth/logout", methods=["POST"])
@require_rep
def rep_logout():
    token = (request.headers.get("X-Rep-Token") or "").strip()
    with db_session() as conn:
        conn.execute("DELETE FROM rep_sessions WHERE token = ?", (token,))
    return jsonify({"ok": True})


@app.route("/api/auth/me")
@require_rep
def rep_me():
    with db_session() as conn:
        rep = _rep_with_dealer(conn, _current_rep_id())
    return jsonify(public_rep(rep))


@app.route("/api/auth/change-password", methods=["POST"])
@require_rep
def change_password():
    body = request.get_json(force=True)
    rep_id = _current_rep_id()
    current_password = body.get("current_password") or ""
    new_password = body.get("new_password") or ""

    if not current_password or not new_password:
        return jsonify({"error": "current_password, new_password required"}), 400
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

        if new_password == rep["employee_code"]:
            return jsonify(
                {"error": "PASSWORD_TOO_SIMPLE", "message": "고유ID와 다른 비밀번호를 정해주세요."}
            ), 400

        conn.execute(
            "UPDATE reps SET password_hash = ?, must_change_password = 0 WHERE id = ?",
            (hash_password(new_password), rep_id),
        )
        # 비밀번호를 바꾸면 이 기기만 남기고 다른 로그인은 끊는다.
        token = (request.headers.get("X-Rep-Token") or "").strip()
        conn.execute(
            "DELETE FROM rep_sessions WHERE rep_id = ? AND token != ?", (rep_id, token)
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
        role = (admin["role"] or "super") if admin else ""
        if not admin or role not in SKT_ROLES or not check_password_hash(admin["password_hash"], password):
            return jsonify({"error": "INVALID_ADMIN", "message": "관리자 아이디 또는 비밀번호가 올바르지 않습니다."}), 401
        token = _create_session(conn, admin["id"])
        return jsonify(
            {
                "token": token,
                "username": admin["username"],
                "role": role,
                "can_edit": role == "super",
                "must_change_password": bool(admin["must_change_password"]),
            }
        )


@app.route("/api/admin/logout", methods=["POST"])
@require_skt
def admin_logout():
    token = (request.headers.get("X-Admin-Token") or "").strip()
    with db_session() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
    return jsonify({"ok": True})


@app.route("/api/admin/me")
@require_skt
def admin_me():
    role = g.admin["role"]
    return jsonify(
        {
            "username": g.admin["username"],
            "role": role,
            "can_edit": role == "super",
            "must_change_password": bool(g.admin.get("must_change_password")),
        }
    )


@app.route("/api/inventory/login", methods=["POST"])
def inventory_login():
    """SKT 계정(아이디) 또는 대리점 직원(사원 고유ID) 모두 이 로그인을 쓴다."""
    body = request.get_json(force=True)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        return jsonify({"error": "username, password required", "message": "아이디와 비밀번호를 입력해주세요."}), 400

    invalid = (
        jsonify({"error": "INVALID_LOGIN", "message": "아이디 또는 비밀번호가 올바르지 않습니다."}),
        401,
    )
    with db_session() as conn:
        admin = conn.execute("SELECT * FROM admins WHERE username = ?", (username,)).fetchone()
        if admin and (admin["role"] or "super") in SKT_ROLES:
            if not check_password_hash(admin["password_hash"], password):
                return invalid
            token = _create_session(conn, admin["id"])
            return jsonify(_user_payload(_admin_from_token(conn, token), token))

        rep = conn.execute(
            """
            SELECT r.*, d.dealer_code, d.name AS dealer_name
            FROM reps r LEFT JOIN dealers d ON d.id = r.dealer_id
            WHERE r.employee_code = ?
            """,
            (username,),
        ).fetchone()
        if not rep or not rep["password_hash"] or not check_password_hash(rep["password_hash"], password):
            return invalid
        if not rep["dealer_id"]:
            return jsonify(
                {
                    "error": "NO_DEALER",
                    "message": "소속 대리점이 없는 계정이라 재고 화면을 쓸 수 없습니다. 관리자에게 문의하세요.",
                }
            ), 403
        token = _create_rep_session(conn, rep["id"])
        return jsonify(_user_payload(_dealer_user_from_rep(dict(rep)), token))


@app.route("/api/inventory/logout", methods=["POST"])
@require_inventory_user
def inventory_logout():
    token = _request_token()
    with db_session() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
        conn.execute("DELETE FROM rep_sessions WHERE token = ?", (token,))
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
@require_skt
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
            "UPDATE admins SET password_hash = ?, must_change_password = 0 WHERE id = ?",
            (hash_password(new_password), admin["id"]),
        )
        return jsonify({"ok": True, "message": "비밀번호가 변경되었습니다."})


@app.route("/api/admin/settings", methods=["GET", "POST"])
@require_skt
def admin_settings():
    with db_session() as conn:
        if request.method == "GET":
            defaults = _point_defaults(conn)
            return jsonify({"points_normal": defaults["normal"], "points_rare": defaults["rare"]})

        if not _is_super():
            return jsonify({"error": "SUPER_ONLY", "message": "SKT 총괄 계정만 포인트를 바꿀 수 있습니다."}), 403
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
@require_skt
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
@require_skt
def download_admin_stats():
    with db_session() as conn:
        data = build_stats_xlsx(conn)
    stamp = kst_now().strftime("%Y%m%d")
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=RS_Treasure_stats_{stamp}.xlsx"},
    )


# ---------------------------------------------------------------------------
# 대리점 / 사원
# ---------------------------------------------------------------------------


@app.route("/api/dealers")
@require_skt
def list_dealers():
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM dealers ORDER BY name").fetchall()
        return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/reps")
@require_skt
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


@app.route("/api/reps/<rep_id>/dealer-role", methods=["PATCH"])
@require_skt
def update_rep_dealer_role(rep_id):
    """대리점 관리자/직원 구분. 권한은 같고 표시용 구분이라 SKT 직원도 바꿀 수 있다."""
    body = request.get_json(force=True, silent=True) or {}
    dealer_role = (body.get("dealer_role") or "").strip()
    if dealer_role not in DEALER_ROLES:
        return jsonify({"error": "BAD_ROLE", "message": "manager 또는 staff 여야 합니다."}), 400
    with db_session() as conn:
        rep = conn.execute("SELECT id FROM reps WHERE id = ?", (rep_id,)).fetchone()
        if not rep:
            return jsonify({"error": "REP_NOT_FOUND"}), 404
        conn.execute("UPDATE reps SET dealer_role = ? WHERE id = ?", (dealer_role, rep_id))
        return jsonify(public_rep(_rep_with_dealer(conn, rep_id)))


# ---------------------------------------------------------------------------
# SKT 직원 계정 - 총괄만 만들고 지운다
# ---------------------------------------------------------------------------


def _public_account(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"] or "super",
        "created_at": row["created_at"],
    }


@app.route("/api/admin/accounts")
@require_admin
def list_skt_accounts():
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM admins WHERE role IN ('super', 'staff') ORDER BY role DESC, username"
        ).fetchall()
        return jsonify([_public_account(r) for r in rows])


@app.route("/api/admin/accounts", methods=["POST"])
@require_admin
def create_skt_staff_account():
    body = request.get_json(force=True, silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        return jsonify({"error": "BAD_INPUT", "message": "아이디와 초기 비밀번호를 입력해주세요."}), 400
    if len(password) < 4:
        return jsonify({"error": "PASSWORD_TOO_SHORT", "message": "비밀번호는 4자 이상이어야 합니다."}), 400
    with db_session() as conn:
        if conn.execute("SELECT 1 FROM admins WHERE username = ?", (username,)).fetchone():
            return jsonify({"error": "USERNAME_EXISTS", "message": "이미 있는 아이디입니다."}), 409
        # 재고 화면은 같은 로그인 칸에 사원 고유ID도 받으므로 겹치면 안 된다.
        if conn.execute("SELECT 1 FROM reps WHERE employee_code = ?", (username,)).fetchone():
            return jsonify(
                {"error": "USERNAME_EXISTS", "message": "영업사원 고유ID와 같은 아이디는 쓸 수 없습니다."}
            ), 409
        account_id = new_id()
        # 발급받은 초기 비밀번호는 첫 로그인 때 본인이 바꾼다.
        conn.execute(
            """
            INSERT INTO admins (id, username, password_hash, created_at, role, must_change_password)
            VALUES (?, ?, ?, ?, 'staff', 1)
            """,
            (account_id, username, hash_password(password), now_iso()),
        )
        row = conn.execute("SELECT * FROM admins WHERE id = ?", (account_id,)).fetchone()
        return jsonify(_public_account(row)), 201


@app.route("/api/admin/accounts/<account_id>", methods=["DELETE"])
@require_admin
def delete_skt_staff_account(account_id):
    with db_session() as conn:
        row = conn.execute("SELECT * FROM admins WHERE id = ?", (account_id,)).fetchone()
        if not row:
            return jsonify({"error": "ACCOUNT_NOT_FOUND"}), 404
        if row["role"] != "staff":
            return jsonify({"error": "CANNOT_DELETE", "message": "SKT 직원 계정만 삭제할 수 있습니다."}), 400
        conn.execute("DELETE FROM admin_sessions WHERE admin_id = ?", (account_id,))
        conn.execute("DELETE FROM admins WHERE id = ?", (account_id,))
        return jsonify({"ok": True})


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
@require_rep
def get_rep(rep_id):
    if rep_id != _current_rep_id():
        return jsonify({"error": "FORBIDDEN"}), 403
    with db_session() as conn:
        rep = _rep_with_dealer(conn, rep_id)
        if not rep:
            return jsonify({"error": "REP_NOT_FOUND"}), 404
        return jsonify(public_rep(rep))


# ---------------------------------------------------------------------------
# 판매점(Store) - 관리자용
# ---------------------------------------------------------------------------


@app.route("/api/stores", methods=["GET"])
@require_skt
def list_stores():
    """판매점이 12,545곳이라 검색어가 없으면 최근 것만, 있으면 그 검색 결과만 내려준다.

    관리자 화면이 매번 전체를 내려받아 30곳만 보여주던 걸 고치는 것.
    """
    q = (request.args.get("q") or "").strip()
    try:
        limit = int(request.args.get("limit", 30))
    except (TypeError, ValueError):
        limit = 30
    limit = min(max(limit, 1), 200)

    with db_session() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM stores").fetchone()["c"]

        where = ""
        params: list = []
        if q:
            like = f"%{q}%"
            where = """
                WHERE s.name LIKE ? OR s.store_code LIKE ? OR s.address LIKE ?
                   OR s.detail_address LIKE ? OR d.name LIKE ? OR d.dealer_code LIKE ?
            """
            params = [like, like, like, like, like, like]

        rows = conn.execute(
            f"""
            SELECT s.*, d.dealer_code, d.name as dealer_name
            FROM stores s
            LEFT JOIN dealers d ON d.id = s.dealer_id
            {where}
            ORDER BY s.created_at DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()

        matched = total
        if q:
            matched = conn.execute(
                f"""
                SELECT COUNT(*) AS c
                FROM stores s
                LEFT JOIN dealers d ON d.id = s.dealer_id
                {where}
                """,
                params,
            ).fetchone()["c"]

        return jsonify(
            {
                "items": [row_to_dict(r) for r in rows],
                "total": total,
                "matched": matched,
                "limit": limit,
                "query": q,
            }
        )


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
    store_code = normalize_store_code(body.get("store_code"))
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
@require_rep
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
@require_skt
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
@require_rep
def start_visit_session():
    body = request.get_json(force=True)
    rep_id = _current_rep_id()
    store_id = body.get("store_id")
    device_id = body.get("device_id")
    if not store_id:
        return jsonify({"error": "store_id required"}), 400

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
@require_rep
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
            "SELECT * FROM visit_sessions WHERE id = ? AND rep_id = ?",
            (session_id, _current_rep_id()),
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
@require_rep
def complete_visit_session(session_id):
    with db_session() as conn:
        session = conn.execute(
            "SELECT * FROM visit_sessions WHERE id = ? AND rep_id = ?",
            (session_id, _current_rep_id()),
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
            WHERE vs.rep_id = ? AND vs.id != ?
                  AND vs.status IN ('auto_approved', 'pending_review', 'manual_approved')
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

        start_of_day = kst_day_start_utc_iso()
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
            point_ledger_entry, claimed_treasure = _grant_visit_points(
                conn, session_id, session["rep_id"], store
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
# 검토 대기(pending_review) 방문 처리 - 관리자
# ---------------------------------------------------------------------------

REVIEW_REASON_LABELS = {
    "R2_PARTIAL_RADIUS_COVERAGE": "반경 30m 밖 위치가 섞임",
    "R3_LOW_GPS_ACCURACY": "GPS 오차가 큼",
    "R5_MOVEMENT_INCONSISTENCY": "세션 중 이동이 도보보다 빠름",
    "R7_ALREADY_CLAIMED_TODAY": "오늘 같은 매장 중복 인증",
    "R8_DEVICE_MISMATCH": "등록된 기기와 다름",
    "R9_OFF_HOURS_ACTIVITY": "근무시간(06~22시) 외",
}


def _review_reasons(raw: str) -> list[dict]:
    try:
        codes = json.loads(raw or "[]")
    except (TypeError, ValueError):
        codes = []
    return [{"code": c, "label": REVIEW_REASON_LABELS.get(c, c)} for c in codes]


@app.route("/api/admin/visit-sessions")
@require_skt
def list_review_sessions():
    """검토 대기 방문 목록. require_admin 이라 본사 계정만 들어온다."""
    status = (request.args.get("status") or "pending_review").strip()
    if status not in {"pending_review", "rejected", "auto_approved", "manual_approved", "manual_rejected"}:
        return jsonify({"error": "BAD_STATUS"}), 400
    limit = min(int(request.args.get("limit", 100) or 100), 500)

    sql = """
        SELECT vs.*, r.name AS rep_name, r.employee_code, d.name AS dealer_name,
               s.name AS store_name, s.address, s.lat AS store_lat, s.lng AS store_lng,
               (SELECT COUNT(*) FROM location_samples ls WHERE ls.session_id = vs.id) AS sample_count,
               (SELECT MIN(ls.accuracy) FROM location_samples ls WHERE ls.session_id = vs.id) AS best_accuracy
        FROM visit_sessions vs
        JOIN reps r ON r.id = vs.rep_id
        LEFT JOIN dealers d ON d.id = r.dealer_id
        JOIN stores s ON s.id = vs.store_id
        WHERE vs.status = ?
    """
    params: list = [status]
    sql += " ORDER BY vs.ended_at DESC LIMIT ?"
    params.append(limit)

    with db_session() as conn:
        rows = conn.execute(sql, params).fetchall()
        items = []
        for row in rows:
            item = row_to_dict(row)
            item["reasons"] = _review_reasons(row["flag_reasons"])
            sample = conn.execute(
                """
                SELECT lat, lng FROM location_samples
                WHERE session_id = ? ORDER BY captured_at LIMIT 1
                """,
                (row["id"],),
            ).fetchone()
            if sample and row["store_lat"] is not None:
                item["first_sample_distance_m"] = round(
                    haversine_distance_meters(
                        sample["lat"], sample["lng"], row["store_lat"], row["store_lng"]
                    )
                )
            else:
                item["first_sample_distance_m"] = None
            items.append(item)
        return jsonify({"status": status, "items": items})


@app.route("/api/admin/visit-sessions/<session_id>/review", methods=["POST"])
@require_admin
def review_visit_session(session_id):
    """검토 대기 방문을 승인/반려한다. 승인하면 자동 승인과 같은 규칙으로 포인트를 준다."""
    body = request.get_json(force=True, silent=True) or {}
    decision = (body.get("decision") or "").strip()
    if decision not in {"approve", "reject"}:
        return jsonify({"error": "BAD_DECISION", "message": "approve 또는 reject 여야 합니다."}), 400

    reviewer = (getattr(g, "admin", None) or {}).get("username") or "admin"

    with db_session() as conn:
        row = conn.execute("SELECT * FROM visit_sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            return jsonify({"error": "SESSION_NOT_FOUND"}), 404
        if row["status"] != "pending_review":
            return jsonify({"error": "NOT_PENDING", "message": "검토 대기 상태가 아닙니다."}), 409

        store = conn.execute("SELECT * FROM stores WHERE id = ?", (row["store_id"],)).fetchone()
        new_status = "manual_approved" if decision == "approve" else "manual_rejected"
        conn.execute(
            """
            UPDATE visit_sessions
            SET status = ?, reviewed_at = ?, reviewed_by = ?
            WHERE id = ? AND status = 'pending_review'
            """,
            (new_status, now_iso(), reviewer, session_id),
        )

        point_ledger_entry = None
        note = ""
        if decision == "approve":
            codes = {r["code"] for r in _review_reasons(row["flag_reasons"])}
            already = conn.execute(
                "SELECT id FROM point_ledger WHERE session_id = ?", (session_id,)
            ).fetchone()
            if already:
                note = "이미 포인트가 적립된 방문입니다."
            elif "R7_ALREADY_CLAIMED_TODAY" in codes:
                # 같은 매장 당일 중복은 인증만 인정하고 포인트는 주지 않는다(R7).
                note = "오늘 같은 매장에서 이미 적립해 포인트는 지급하지 않았습니다."
            else:
                point_ledger_entry, _ = _grant_visit_points(conn, session_id, row["rep_id"], store)

        session = row_to_dict(
            conn.execute("SELECT * FROM visit_sessions WHERE id = ?", (session_id,)).fetchone()
        )
        return jsonify({"session": session, "point_ledger_entry": point_ledger_entry, "note": note})


# ---------------------------------------------------------------------------
# 포인트 / 랭킹
# ---------------------------------------------------------------------------


@app.route("/api/points/<rep_id>")
@require_rep
def get_points(rep_id):
    if rep_id != _current_rep_id():
        return jsonify({"error": "FORBIDDEN", "message": "본인 포인트만 볼 수 있습니다."}), 403
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM point_ledger WHERE rep_id = ? ORDER BY created_at DESC", (rep_id,)
        ).fetchall()
        ledgers = [row_to_dict(r) for r in rows]
        wallet = _rep_point_balance(conn, rep_id)

        # 소속 대리점 안에서 내 누적 적립 순위. 대리점이 없으면 순위도 없다.
        dealer_rank = None
        dealer_rep_count = None
        rep_row = conn.execute("SELECT dealer_id FROM reps WHERE id = ?", (rep_id,)).fetchone()
        dealer_id = rep_row["dealer_id"] if rep_row else None
        if dealer_id:
            peers = conn.execute(
                """
                SELECT r.id as rep_id, COALESCE(SUM(pl.points), 0) as total_points
                FROM reps r
                LEFT JOIN point_ledger pl ON pl.rep_id = r.id
                WHERE r.dealer_id = ?
                GROUP BY r.id
                ORDER BY total_points DESC
                """,
                (dealer_id,),
            ).fetchall()
            dealer_rep_count = len(peers)
            for i, row in enumerate(peers):
                if row["rep_id"] == rep_id:
                    dealer_rank = i + 1
                    break

        # total 은 기존 화면 호환을 위해 누적 적립 그대로 두고, 사용/잔액을 함께 내려준다.
        return jsonify(
            {
                "total": wallet["earned"],
                "used": wallet["spent"],
                "balance": wallet["balance"],
                "ledgers": ledgers,
                "dealer_rank": dealer_rank,
                "dealer_rep_count": dealer_rep_count,
            }
        )


@app.route("/api/points")
@require_skt
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
@require_rep
def public_rankings():
    """영업사원 화면용. 대리점/사원 상위 10. 사원 이름은 마스킹한다."""
    me_id = _current_rep_id()
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
@require_rep
def request_reward():
    body = request.get_json(force=True)
    rep_id = _current_rep_id()
    reward_type = body.get("type")
    try:
        point_cost = int(body["point_cost"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "type, point_cost required"}), 400
    if not reward_type or point_cost <= 0:
        return jsonify({"error": "type, point_cost required"}), 400

    with db_session() as conn:
        # 이미 신청한 리워드만큼 차감한 잔액으로 판단한다.
        wallet = _rep_point_balance(conn, rep_id)
        if wallet["balance"] < point_cost:
            return jsonify(
                {
                    "error": "INSUFFICIENT_POINTS",
                    "message": f"사용 가능한 포인트가 부족합니다. (보유 {wallet['balance']}P)",
                    "total_points": wallet["earned"],
                    "used_points": wallet["spent"],
                    "balance": wallet["balance"],
                }
            ), 400

        reward_id = new_id()
        conn.execute(
            "INSERT INTO rewards (id, rep_id, type, point_cost, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
            (reward_id, rep_id, reward_type, point_cost, now_iso()),
        )
        reward = conn.execute("SELECT * FROM rewards WHERE id = ?", (reward_id,)).fetchone()
        payload = row_to_dict(reward)
        payload["wallet"] = _rep_point_balance(conn, rep_id)
        return jsonify(payload), 201


@app.route("/api/rewards/<rep_id>")
@require_rep
def list_rewards(rep_id):
    if rep_id != _current_rep_id():
        return jsonify({"error": "FORBIDDEN", "message": "본인 리워드만 볼 수 있습니다."}), 403
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
@require_skt
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
@require_inventory_uploader
def import_inventory():
    upload = request.files.get("file") or (request.files.getlist("files") or [None])[0]
    if not upload:
        return jsonify({"error": "재고현황 xlsx 파일을 올려주세요."}), 400
    filename = upload.filename or "inventory.xlsx"
    lower = filename.lower()
    if not (lower.endswith(".xlsx") or lower.endswith(".csv")):
        return jsonify({"error": f"{filename}: .xlsx 또는 .csv 만 지원합니다."}), 400
    job_id = _normalize_upload_job_id(request.form.get("job_id") or "") or new_id()
    user = getattr(g, "inventory_user", None) or {}
    now = time.time()
    with _UPLOAD_LOCK:
        current = _UPLOAD_JOBS.get(job_id) or {}
        status = current.get("status") or ""
        age = now - float(current.get("started_at") or 0)
        if status in {"queued", "parsing", "saving", "done"}:
            return jsonify(_upload_job_payload(dict(current))), 202
        if status == "receiving" and age < 45:
            return jsonify(_upload_job_payload(dict(current))), 202
        job = _UPLOAD_JOBS.setdefault(job_id, {"job_id": job_id})
        job.update(
            status="receiving",
            message="파일을 받는 중...",
            owner_id=user.get("id") or "",
            filename=filename,
            started_at=now,
        )
    data = upload.read()
    if not data:
        _upload_job_set(job_id, status="error", message="빈 파일입니다.")
        return jsonify({"error": "빈 파일입니다."}), 400
    dealer = None
    scoped = _scoped_dealer_id()
    if scoped:
        if not user.get("dealer_id"):
            _upload_job_set(job_id, status="error", message="대리점 정보를 찾지 못했습니다.")
            return jsonify({"error": "대리점 정보를 찾지 못했습니다."}), 400
        dealer = {
            "id": user.get("dealer_id") or "",
            "dealer_code": user.get("dealer_code") or "",
            "name": user.get("dealer_name") or "",
        }
    _upload_job_set(job_id, status="queued", message="업로드를 시작했습니다.")
    threading.Thread(
        target=_run_inventory_upload,
        args=(job_id, filename, data, dealer),
        daemon=True,
        name=f"inventory-upload-{job_id[:8]}",
    ).start()
    return jsonify({"job_id": job_id, "status": "queued", "message": "파일을 받은 뒤 저장하고 있습니다."}), 202


@app.route("/api/inventory/excel/status")
@require_inventory_user
def inventory_upload_status():
    # 로그인은 요구하되, 예전처럼 작업 소유자까지 따지지는 않는다.
    # (재로그인하면 소유자 id가 달라져 진행 중인 업로드를 못 보는 문제가 있었다.)
    job_id = _normalize_upload_job_id(request.args.get("job_id") or "")
    job = _upload_job_get(job_id)
    if not job:
        return jsonify({"error": "JOB_NOT_FOUND", "message": "업로드 상태를 찾지 못했습니다."}), 404
    return jsonify(_upload_job_payload(job))


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
@require_skt
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
