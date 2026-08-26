import os
import shutil
import sqlite3
from contextlib import contextmanager

_BASE_DIR = os.path.dirname(__file__)
# Render/Playground 컨테이너는 앱 디렉터리가 읽기전용일 수 있어, 운영 DB는 /tmp 를 쓴다.
_on_hosted = bool(os.environ.get("RENDER") or os.environ.get("CONTEXT_PATH"))
_DEFAULT_DB = (
    os.path.join("/tmp", "rs_treasure.db")
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
    created_at TEXT NOT NULL
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
    created_at TEXT NOT NULL
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

CREATE TABLE IF NOT EXISTS visit_sessions (
    id TEXT PRIMARY KEY,
    rep_id TEXT NOT NULL REFERENCES reps(id),
    store_id TEXT NOT NULL REFERENCES stores(id),
    device_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    confidence_score REAL,
    status TEXT NOT NULL DEFAULT 'in_progress',
    flag_reasons TEXT NOT NULL DEFAULT '[]'
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
CREATE INDEX IF NOT EXISTS idx_treasures_store ON treasures(store_id);
CREATE INDEX IF NOT EXISTS idx_treasures_unclaimed ON treasures(claimed_at);
CREATE INDEX IF NOT EXISTS idx_samples_session ON location_samples(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_rep ON visit_sessions(rep_id);
CREATE INDEX IF NOT EXISTS idx_ledger_rep ON point_ledger(rep_id);
CREATE INDEX IF NOT EXISTS idx_inventory_store ON inventory_items(store_code);
CREATE INDEX IF NOT EXISTS idx_inventory_product ON inventory_items(product_short);
CREATE INDEX IF NOT EXISTS idx_inventory_holder ON inventory_items(holder_type);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


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

    # 기존 사원 중 비밀번호가 없으면 초기 비밀번호 = 고유ID
    for row in conn.execute(
        "SELECT id, employee_code FROM reps WHERE password_hash IS NULL OR password_hash = ''"
    ).fetchall():
        conn.execute(
            "UPDATE reps SET password_hash = ? WHERE id = ?",
            (generate_password_hash(row["employee_code"]), row["id"]),
        )

    # 테스트 계정 1107711(고바야시)은 소속 대리점 없음
    conn.execute("UPDATE reps SET dealer_id = NULL WHERE employee_code = '1107711'")

    treasure_cols = _columns(conn, "treasures")
    if treasure_cols and "points" not in treasure_cols:
        conn.execute("ALTER TABLE treasures ADD COLUMN points INTEGER")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS admins (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
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

    # 최초 1회만 기본 관리자를 만든다. 이미 있으면 비밀번호를 덮어쓰지 않는다.
    admin_username = (os.environ.get("ADMIN_USERNAME") or "admin").strip() or "admin"
    admin_password = os.environ.get("ADMIN_INITIAL_PASSWORD") or "admin"
    existing_admin = conn.execute("SELECT id FROM admins WHERE username = ?", (admin_username,)).fetchone()
    if not existing_admin:
        from datetime import datetime

        conn.execute(
            "INSERT INTO admins (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (
                "admin-default",
                admin_username,
                generate_password_hash(admin_password),
                datetime.utcnow().isoformat(),
            ),
        )


def init_db() -> None:
    _ensure_db_file()
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        migrate_schema(conn)
        conn.commit()
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
