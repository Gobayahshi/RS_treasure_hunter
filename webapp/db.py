import os
import shutil
import sqlite3
from contextlib import contextmanager

_BASE_DIR = os.path.dirname(__file__)
# Render 유료 Disk를 /data 에 붙이면 재고/주소가 재시작 후에도 남는다.
# Disk가 없으면 /tmp (배포마다 초기화).
_on_hosted = bool(os.environ.get("RENDER") or os.environ.get("CONTEXT_PATH"))
_persistent_dir = "/data" if os.path.isdir("/data") else "/tmp"
_DEFAULT_DB = (
    os.path.join(_persistent_dir, "rs_treasure.db")
    if _on_hosted
    else os.path.join(_BASE_DIR, "rs_treasure.db")
)
DB_PATH = os.environ.get("DB_PATH") or _DEFAULT_DB
SEED_DB_PATH = os.path.join(_BASE_DIR, "seed", "rs_treasure.db")


def _ensure_db_file() -> None:
    """배포 환경에 DB가 없으면 시드 DB를 복사한다."""
    if os.path.exists(DB_PATH):
        return
    if os.path.exists(SEED_DB_PATH):
        parent = os.path.dirname(DB_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.copy2(SEED_DB_PATH, DB_PATH)

SCHEMA = """
CREATE TABLE IF NOT EXISTS dealers (
    id TEXT PRIMARY KEY,
    dealer_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stores (
    id TEXT PRIMARY KEY,
    dealer_id TEXT REFERENCES dealers(id),
    store_code TEXT,
    name TEXT NOT NULL,
    address TEXT NOT NULL,
    detail_address TEXT,
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reps (
    id TEXT PRIMARY KEY,
    dealer_id TEXT REFERENCES dealers(id),
    name TEXT NOT NULL,
    employee_code TEXT NOT NULL UNIQUE,
    password_hash TEXT,
    device_id TEXT,
    created_at TEXT NOT NULL,
    dealer_role TEXT NOT NULL DEFAULT 'staff',
    phone_last4 TEXT,
    password_reset_at TEXT,
    must_change_password INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS treasures (
    id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL REFERENCES stores(id),
    tier TEXT NOT NULL DEFAULT 'normal',
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    active_date TEXT NOT NULL,
    claimed_at TEXT,
    claimed_session_id TEXT,
    points INTEGER
);

CREATE TABLE IF NOT EXISTS admins (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    dealer_id TEXT,
    role TEXT NOT NULL DEFAULT 'super',
    must_change_password INTEGER NOT NULL DEFAULT 0,
    name TEXT
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token TEXT PRIMARY KEY,
    admin_id TEXT NOT NULL REFERENCES admins(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rep_sessions (
    token TEXT PRIMARY KEY,
    rep_id TEXT NOT NULL REFERENCES reps(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visit_sessions (
    id TEXT PRIMARY KEY,
    rep_id TEXT NOT NULL REFERENCES reps(id),
    store_id TEXT NOT NULL REFERENCES stores(id),
    device_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    confidence_score REAL,
    status TEXT NOT NULL DEFAULT 'in_progress',
    flag_reasons TEXT NOT NULL DEFAULT '[]',
    reviewed_at TEXT,
    reviewed_by TEXT
);

CREATE TABLE IF NOT EXISTS location_samples (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES visit_sessions(id),
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    accuracy REAL NOT NULL,
    is_mock INTEGER NOT NULL DEFAULT 0,
    captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS point_ledger (
    id TEXT PRIMARY KEY,
    rep_id TEXT NOT NULL REFERENCES reps(id),
    session_id TEXT UNIQUE REFERENCES visit_sessions(id),
    points INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rewards (
    id TEXT PRIMARY KEY,
    rep_id TEXT NOT NULL REFERENCES reps(id),
    type TEXT NOT NULL,
    point_cost INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    issued_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory_uploads (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    as_of_date TEXT,
    row_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    dealer_id TEXT,
    dealer_code TEXT,
    dealer_name TEXT
);

CREATE TABLE IF NOT EXISTS inventory_items (
    id TEXT PRIMARY KEY,
    upload_id TEXT NOT NULL REFERENCES inventory_uploads(id),
    store_code TEXT NOT NULL,
    holder_name TEXT,
    holder_type TEXT NOT NULL,
    product_short TEXT,
    model_name TEXT,
    purchase_price TEXT,
    inbound_date TEXT,
    moved_date TEXT,
    hold_days INTEGER,
    serial TEXT,
    dealer_id TEXT,
    dealer_code TEXT,
    dealer_name TEXT
);

-- 판매점이 수천 건이라 주변 검색/스폰에 필요한 인덱스를 둔다.
CREATE INDEX IF NOT EXISTS idx_stores_latlng ON stores(lat, lng);
CREATE INDEX IF NOT EXISTS idx_stores_address ON stores(address);
-- 재고 조인용. migrate_schema 의 idx_stores_code 는 부분 인덱스라
-- LEFT JOIN 에서는 쓰이지 못해, 조건 없는 인덱스를 따로 둔다.
CREATE INDEX IF NOT EXISTS idx_stores_code_lookup ON stores(store_code);
CREATE INDEX IF NOT EXISTS idx_treasures_store ON treasures(store_id);
CREATE INDEX IF NOT EXISTS idx_treasures_unclaimed ON treasures(claimed_at);
CREATE INDEX IF NOT EXISTS idx_samples_session ON location_samples(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_rep ON visit_sessions(rep_id);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON visit_sessions(status, ended_at);
CREATE INDEX IF NOT EXISTS idx_ledger_rep ON point_ledger(rep_id);
CREATE INDEX IF NOT EXISTS idx_rep_sessions_rep ON rep_sessions(rep_id);
CREATE INDEX IF NOT EXISTS idx_inventory_store ON inventory_items(store_code);
CREATE INDEX IF NOT EXISTS idx_inventory_product ON inventory_items(product_short);
CREATE INDEX IF NOT EXISTS idx_inventory_holder ON inventory_items(holder_type);
-- 지도/집계는 항상 "최신 업로드 + 보유처 종류"로 먼저 걸러낸다.
CREATE INDEX IF NOT EXISTS idx_inventory_upload ON inventory_items(upload_id, holder_type);
CREATE INDEX IF NOT EXISTS idx_inventory_dealer ON inventory_items(dealer_id);
CREATE INDEX IF NOT EXISTS idx_uploads_dealer ON inventory_uploads(dealer_id, created_at);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL은 -shm 공유메모리를 mmap으로 매핑하는데, Render 영구 디스크(네트워크 블록 스토리지)에서
    # 이게 disk I/O error를 일으켰다 (2026-09-17). 일반 롤백 저널은 mmap 없이 파일 잠금만 쓴다.
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _remove_dealer_portal_accounts(conn) -> None:
    """예전 임시 대리점 계정(yuwon/frisbee/jieun 등)을 지운다.

    대리점 사람은 이제 사원 고유ID(reps)로 재고 화면에 들어온다.
    대리점 데이터(dealers)와 올라간 재고는 그대로 둔다.
    """
    rows = conn.execute(
        """
        SELECT id FROM admins
        WHERE role = 'dealer' OR (dealer_id IS NOT NULL AND dealer_id != '')
        """
    ).fetchall()
    for row in rows:
        conn.execute("DELETE FROM admin_sessions WHERE admin_id = ?", (row["id"],))
        conn.execute("DELETE FROM admins WHERE id = ?", (row["id"],))


def migrate_schema(conn) -> None:
    """이미 만들어진 DB에도 대리점/비밀번호 컬럼을 추가한다."""
    from werkzeug.security import generate_password_hash

    store_cols = _columns(conn, "stores")
    if store_cols and "dealer_id" not in store_cols:
        conn.execute("ALTER TABLE stores ADD COLUMN dealer_id TEXT")
    if store_cols and "store_code" not in store_cols:
        conn.execute("ALTER TABLE stores ADD COLUMN store_code TEXT")
    if store_cols and "detail_address" not in store_cols:
        conn.execute("ALTER TABLE stores ADD COLUMN detail_address TEXT")

    # 주소가 같아도 판매점코드가 다르면 다른 매장이다. 기존 DB에도 인덱스를 나중에 만든다.
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_stores_code ON stores(store_code)
        WHERE store_code IS NOT NULL AND store_code != ''
        """
    )

    rep_cols = _columns(conn, "reps")
    if rep_cols and "dealer_id" not in rep_cols:
        conn.execute("ALTER TABLE reps ADD COLUMN dealer_id TEXT")

    if rep_cols and "password_hash" not in rep_cols:
        conn.execute("ALTER TABLE reps ADD COLUMN password_hash TEXT")

    # 대리점 관리자/직원 구분 (권한은 같다). SKT 총괄·직원이 관리 화면에서 정한다.
    if rep_cols and "dealer_role" not in rep_cols:
        conn.execute("ALTER TABLE reps ADD COLUMN dealer_role TEXT NOT NULL DEFAULT 'staff'")

    # 비밀번호 본인 재설정용. 개인정보를 줄이려고 전화번호는 뒤 4자리만 저장한다.
    if rep_cols and "phone_last4" not in rep_cols:
        conn.execute("ALTER TABLE reps ADD COLUMN phone_last4 TEXT")
        if "phone" in rep_cols:
            # 예전에 저장한 전체 번호는 뒤 4자리만 남기고 지운다.
            conn.execute(
                """
                UPDATE reps SET phone_last4 = SUBSTR(REPLACE(REPLACE(REPLACE(phone, '-', ''), ' ', ''), '+', ''), -4)
                WHERE phone IS NOT NULL AND phone != ''
                """
            )
            conn.execute("UPDATE reps SET phone = NULL")
    if rep_cols and "password_reset_at" not in rep_cols:
        conn.execute("ALTER TABLE reps ADD COLUMN password_reset_at TEXT")

    # 고유ID는 대문자로 통일한다 (로그인은 대소문자를 가리지 않는다).
    # 대문자로 바꾸면 겹치는 ID가 생기는 경우에는 건드리지 않는다.
    if rep_cols:
        clash = conn.execute(
            """
            SELECT 1 FROM reps GROUP BY UPPER(employee_code) HAVING COUNT(*) > 1 LIMIT 1
            """
        ).fetchone()
        if not clash:
            conn.execute("UPDATE reps SET employee_code = UPPER(employee_code) WHERE employee_code != UPPER(employee_code)")

    # 초기 비밀번호(=고유ID)를 쓰는 사람은 바꿀 때까지 앱을 못 쓰게 막는다.
    # 매 요청마다 해시를 비교하면 느리므로 플래그로 들고 있는다.
    if rep_cols and "must_change_password" not in rep_cols:
        from werkzeug.security import check_password_hash

        conn.execute("ALTER TABLE reps ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
        # 컬럼을 처음 만들 때 한 번만, 지금 초기 비밀번호를 쓰는 사람을 표시한다.
        for row in conn.execute("SELECT id, employee_code, password_hash FROM reps").fetchall():
            stored = row["password_hash"] or ""
            if stored and check_password_hash(stored, row["employee_code"]):
                conn.execute("UPDATE reps SET must_change_password = 1 WHERE id = ?", (row["id"],))

    # 기존 사원 중 비밀번호가 없으면 초기 비밀번호 = 고유ID
    for row in conn.execute(
        "SELECT id, employee_code FROM reps WHERE password_hash IS NULL OR password_hash = ''"
    ).fetchall():
        conn.execute(
            "UPDATE reps SET password_hash = ? WHERE id = ?",
            (generate_password_hash(row["employee_code"]), row["id"]),
        )

    # 테스트 계정 1107711(고바야시)은 소속 대리점 없음
    try:
        conn.execute("UPDATE reps SET dealer_id = NULL WHERE employee_code = '1107711'")
    except sqlite3.OperationalError:
        pass

    treasure_cols = _columns(conn, "treasures")
    if treasure_cols and "points" not in treasure_cols:
        conn.execute("ALTER TABLE treasures ADD COLUMN points INTEGER")

    # 판매점코드를 공백 없는 대문자로 맞춘다. 조인이 인덱스를 타려면 양쪽 형식이 같아야 한다.
    # 정규화하면 코드가 겹치는 행이 있을 수 있는데(유니크 인덱스 위반), 그때는 건너뛰고 부팅은 계속한다.
    for table in ("stores", "inventory_items"):
        try:
            conn.execute(
                f"""
                UPDATE {table} SET store_code = UPPER(REPLACE(TRIM(store_code), ' ', ''))
                WHERE store_code IS NOT NULL
                  AND store_code <> UPPER(REPLACE(TRIM(store_code), ' ', ''))
                """
            )
        except sqlite3.Error:
            pass  # 재고 테이블이 아직 없거나, 정규화 시 코드가 겹치는 경우

    # 검토 대기 방문을 관리자가 승인/반려한 기록
    session_cols = _columns(conn, "visit_sessions")
    if session_cols and "reviewed_at" not in session_cols:
        conn.execute("ALTER TABLE visit_sessions ADD COLUMN reviewed_at TEXT")
    if session_cols and "reviewed_by" not in session_cols:
        conn.execute("ALTER TABLE visit_sessions ADD COLUMN reviewed_by TEXT")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS admins (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            dealer_id TEXT,
            role TEXT NOT NULL DEFAULT 'super'
        );
        CREATE TABLE IF NOT EXISTS admin_sessions (
            token TEXT PRIMARY KEY,
            admin_id TEXT NOT NULL REFERENCES admins(id),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('points_normal', '10')")
    conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('points_rare', '30')")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS inventory_uploads (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            as_of_date TEXT,
            row_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            dealer_id TEXT,
            dealer_code TEXT,
            dealer_name TEXT
        );
        CREATE TABLE IF NOT EXISTS inventory_items (
            id TEXT PRIMARY KEY,
            upload_id TEXT NOT NULL REFERENCES inventory_uploads(id),
            store_code TEXT NOT NULL,
            holder_name TEXT,
            holder_type TEXT NOT NULL,
            product_short TEXT,
            model_name TEXT,
            purchase_price TEXT,
            inbound_date TEXT,
            moved_date TEXT,
            hold_days INTEGER,
            serial TEXT,
            dealer_id TEXT,
            dealer_code TEXT,
            dealer_name TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_inventory_store ON inventory_items(store_code);
        CREATE INDEX IF NOT EXISTS idx_inventory_product ON inventory_items(product_short);
        CREATE INDEX IF NOT EXISTS idx_inventory_holder ON inventory_items(holder_type);
        """
    )

    for table in ("inventory_uploads", "inventory_items"):
        cols = _columns(conn, table)
        for col in ("dealer_id", "dealer_code", "dealer_name"):
            if cols and col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inventory_dealer ON inventory_items(dealer_id)")

    frisbee = conn.execute("SELECT * FROM dealers WHERE dealer_code = 'D15051'").fetchone()
    if frisbee:
        conn.execute(
            """
            UPDATE inventory_uploads
            SET dealer_id = ?, dealer_code = ?, dealer_name = ?
            WHERE dealer_id IS NULL OR dealer_id = ''
            """,
            (frisbee["id"], frisbee["dealer_code"], frisbee["name"]),
        )
        conn.execute(
            """
            UPDATE inventory_items
            SET dealer_id = ?, dealer_code = ?, dealer_name = ?
            WHERE dealer_id IS NULL OR dealer_id = ''
            """,
            (frisbee["id"], frisbee["dealer_code"], frisbee["name"]),
        )

    admin_cols = _columns(conn, "admins")
    if admin_cols and "must_change_password" not in admin_cols:
        # 총괄이 발급한 초기 비밀번호는 첫 로그인 때 반드시 바꾸게 한다.
        conn.execute("ALTER TABLE admins ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
    if admin_cols and "dealer_id" not in admin_cols:
        conn.execute("ALTER TABLE admins ADD COLUMN dealer_id TEXT")
    if admin_cols and "role" not in admin_cols:
        conn.execute("ALTER TABLE admins ADD COLUMN role TEXT")
    if admin_cols and "name" not in admin_cols:
        # SKT 계정의 실제 이름(예: RS팀 직원 일괄 등록). 없으면 화면에서 username으로 대신한다.
        conn.execute("ALTER TABLE admins ADD COLUMN name TEXT")
    conn.execute("UPDATE admins SET role = 'super' WHERE role IS NULL OR role = ''")

    # 최초 1회만 기본 관리자를 만든다. 이미 있으면 비밀번호를 덮어쓰지 않는다.
    admin_username = (os.environ.get("ADMIN_USERNAME") or "admin").strip() or "admin"
    admin_password = os.environ.get("ADMIN_INITIAL_PASSWORD") or "admin"
    existing_admin = conn.execute("SELECT id FROM admins WHERE username = ?", (admin_username,)).fetchone()
    if not existing_admin:
        from datetime import datetime

        conn.execute(
            "INSERT INTO admins (id, username, password_hash, created_at, role) VALUES (?, ?, ?, ?, ?)",
            (
                "admin-default",
                admin_username,
                generate_password_hash(admin_password),
                datetime.utcnow().isoformat(),
                "super",
            ),
        )

    _remove_dealer_portal_accounts(conn)


def _sync_stores_from_seed(conn) -> None:
    """시드의 판매점 마스터(P코드·이름·주소·좌표)를 배포 DB에 맞춘다."""
    seed_path = os.path.abspath(SEED_DB_PATH)
    live_path = os.path.abspath(DB_PATH)
    if not os.path.exists(SEED_DB_PATH) or os.path.normcase(seed_path) == os.path.normcase(live_path):
        return
    conn.execute("ATTACH DATABASE ? AS seed", (SEED_DB_PATH,))
    try:
        seed_tables = {
            row[0] for row in conn.execute("SELECT name FROM seed.sqlite_master WHERE type='table'")
        }
        if "dealers" in seed_tables:
            conn.execute(
                """
                INSERT OR IGNORE INTO dealers (id, dealer_code, name, created_at)
                SELECT id, dealer_code, name, created_at FROM seed.dealers
                """
            )
        if "stores" not in seed_tables:
            return
        seed_stores = conn.execute(
            """
            SELECT id, dealer_id, store_code, name, address, detail_address, lat, lng, created_at
            FROM seed.stores
            WHERE TRIM(COALESCE(store_code, '')) != ''
            """
        ).fetchall()
        existing = {
            (row["store_code"] or "").strip().upper()
            for row in conn.execute(
                "SELECT store_code FROM stores WHERE TRIM(COALESCE(store_code, '')) != ''"
            )
        }
        updates = []
        inserts = []
        coords = []
        for row in seed_stores:
            code = (row["store_code"] or "").strip().upper()
            if not code:
                continue
            if code in existing:
                updates.append((row["name"], row["address"] or "", row["detail_address"], code))
                if row["lat"] or row["lng"]:
                    coords.append((row["lat"], row["lng"], code))
            else:
                inserts.append(
                    (
                        row["id"],
                        row["dealer_id"],
                        row["store_code"],
                        row["name"],
                        row["address"] or "",
                        row["detail_address"],
                        row["lat"],
                        row["lng"],
                        row["created_at"],
                    )
                )
                existing.add(code)
        if updates:
            conn.executemany(
                """
                UPDATE stores
                SET name = ?,
                    address = COALESCE(NULLIF(?, ''), address),
                    detail_address = COALESCE(?, detail_address)
                WHERE UPPER(TRIM(store_code)) = ?
                """,
                updates,
            )
        if inserts:
            conn.executemany(
                """
                INSERT OR IGNORE INTO stores (
                    id, dealer_id, store_code, name, address, detail_address, lat, lng, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                inserts,
            )
        if coords:
            conn.executemany(
                """
                UPDATE stores SET lat = ?, lng = ?
                WHERE UPPER(TRIM(store_code)) = ? AND lat = 0 AND lng = 0
                """,
                coords,
            )
    finally:
        conn.execute("DETACH DATABASE seed")


def start_store_seed_sync() -> None:
    """Render 포트가 먼저 열리도록 판매점 마스터 동기화는 백그라운드에서 한다."""
    if not _on_hosted:
        return
    import threading

    def _run() -> None:
        conn = None
        try:
            conn = get_conn()
            _sync_stores_from_seed(conn)
            conn.commit()
        except Exception:
            pass
        finally:
            if conn is not None:
                conn.close()

    threading.Thread(target=_run, daemon=True, name="seed-store-sync").start()


def init_db() -> None:
    _ensure_db_file()
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        try:
            migrate_schema(conn)
            conn.commit()
        except sqlite3.OperationalError:
            conn.rollback()
    finally:
        conn.close()


@contextmanager
def db_session():
    """요청 하나당 커넥션 하나를 열고 닫는다. SQLite 파일 하나로 충분한 소규모 파일럿 용도."""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
