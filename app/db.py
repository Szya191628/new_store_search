import hashlib
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from app.config import settings


def hash_pin(pin: str) -> str:
    return hashlib.sha256(f"{settings.app_secret}:{pin}".encode("utf-8")).hexdigest()


class DBAdapter:
    """统一封装 MySQL (pymysql) 与测试用 SQLite，保证业务代码与路由完全一致。"""

    def __init__(self, conn: Any, db_type: str):
        self.conn = conn
        self.db_type = db_type

    def _adapt_sql(self, sql: str) -> str:
        if self.db_type == "sqlite":
            return sql.replace("%s", "?")
        return sql

    def execute(self, sql: str, params: tuple = ()) -> int:
        cur = self.conn.cursor()
        cur.execute(self._adapt_sql(sql), params)
        last_id = cur.lastrowid
        cur.close()
        return last_id

    def fetchone(self, sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(self._adapt_sql(sql), params)
        row = cur.fetchone()
        cur.close()
        if row is None:
            return None
        return dict(row)

    def fetchall(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(self._adapt_sql(sql), params)
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]

    def commit(self) -> None:
        self.conn.commit()


@contextmanager
def get_db():
    if settings.db_type == "sqlite":
        conn = sqlite3.connect(settings.sqlite_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        db = DBAdapter(conn, "sqlite")
        try:
            yield db
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        import pymysql
        from pymysql.cursors import DictCursor

        conn = pymysql.connect(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password,
            database=settings.mysql_database,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
        )
        db = DBAdapter(conn, "mysql")
        try:
            yield db
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db() -> None:
    auto_inc = "INTEGER PRIMARY KEY AUTO_INCREMENT" if settings.db_type == "mysql" else "INTEGER PRIMARY KEY AUTOINCREMENT"
    text_engine = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci" if settings.db_type == "mysql" else ""

    ddl_statements = [
        f"""
        CREATE TABLE IF NOT EXISTS users (
            id {auto_inc},
            username VARCHAR(64) NOT NULL UNIQUE,
            display_name VARCHAR(64) NOT NULL,
            role VARCHAR(16) NOT NULL DEFAULT 'sales',
            pin_hash VARCHAR(128) NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ){text_engine}
        """,
        f"""
        CREATE TABLE IF NOT EXISTS system_settings (
            setting_key VARCHAR(64) PRIMARY KEY,
            setting_value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ){text_engine}
        """,
        f"""
        CREATE TABLE IF NOT EXISTS import_batches (
            id {auto_inc},
            filename VARCHAR(255) NOT NULL,
            total_rows INTEGER NOT NULL DEFAULT 0,
            imported_rows INTEGER NOT NULL DEFAULT 0,
            skipped_dup INTEGER NOT NULL DEFAULT 0,
            filtered_invalid INTEGER NOT NULL DEFAULT 0,
            agency_flagged INTEGER NOT NULL DEFAULT 0,
            uploaded_by INTEGER NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ){text_engine}
        """,
        f"""
        CREATE TABLE IF NOT EXISTS leads (
            id {auto_inc},
            credit_code VARCHAR(64) NULL UNIQUE,
            dedup_key VARCHAR(255) NOT NULL UNIQUE,
            license_name VARCHAR(255) NOT NULL,
            core_trade_name VARCHAR(128) NOT NULL DEFAULT '',
            operator_name VARCHAR(64) NOT NULL DEFAULT '',
            phone VARCHAR(32) NOT NULL DEFAULT '',
            phone_freq INTEGER NOT NULL DEFAULT 1,
            is_agency_phone INTEGER NOT NULL DEFAULT 0,
            address VARCHAR(512) NOT NULL,
            street_name VARCHAR(128) NOT NULL DEFAULT '',
            district VARCHAR(64) NOT NULL DEFAULT '',
            city VARCHAR(64) NOT NULL DEFAULT '',
            reg_date VARCHAR(32) NOT NULL DEFAULT '',
            industry_raw VARCHAR(128) NOT NULL DEFAULT '',
            business_scope TEXT,
            store_track VARCHAR(32) NOT NULL DEFAULT '便民实体型',
            is_chain_brand INTEGER NOT NULL DEFAULT 0,
            lng DOUBLE NULL,
            lat DOUBLE NULL,
            amap_poi_found INTEGER NOT NULL DEFAULT 0,
            amap_poi_name VARCHAR(255) NOT NULL DEFAULT '',
            amap_poi_distance INTEGER NULL,
            amap_match_score INTEGER NOT NULL DEFAULT 0,
            douyin_status VARCHAR(32) NOT NULL DEFAULT 'pending',
            douyin_poi_url TEXT,
            meituan_status VARCHAR(32) NOT NULL DEFAULT 'pending',
            meituan_url TEXT,
            doubao_status VARCHAR(32) NOT NULL DEFAULT 'inferred_missing',
            grade VARCHAR(8) NOT NULL DEFAULT 'S',
            recommended_action VARCHAR(32) NOT NULL DEFAULT 'telesales',
            pitch_script TEXT,
            claim_user_id INTEGER NULL,
            claim_user_name VARCHAR(64) NOT NULL DEFAULT '',
            follow_status VARCHAR(32) NOT NULL DEFAULT '待联系',
            last_note TEXT,
            batch_id INTEGER NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ){text_engine}
        """,
        f"""
        CREATE TABLE IF NOT EXISTS follow_up_logs (
            id {auto_inc},
            lead_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            user_name VARCHAR(64) NOT NULL,
            status_after VARCHAR(32) NOT NULL,
            note TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ){text_engine}
        """,
    ]

    with get_db() as db:
        for ddl in ddl_statements:
            db.execute(ddl)

        # 初始化默认管理员与示例销售账号
        admin = db.fetchone("SELECT id FROM users WHERE username = %s", ("admin",))
        if not admin:
            db.execute(
                "INSERT INTO users (username, display_name, role, pin_hash) VALUES (%s, %s, %s, %s)",
                ("admin", "管理员", "admin", hash_pin("123456")),
            )
            db.execute(
                "INSERT INTO users (username, display_name, role, pin_hash) VALUES (%s, %s, %s, %s)",
                ("sales1", "销售小张", "sales", hash_pin("1234")),
            )
            db.execute(
                "INSERT INTO users (username, display_name, role, pin_hash) VALUES (%s, %s, %s, %s)",
                ("sales2", "销售小李", "sales", hash_pin("1234")),
            )

        # 初始化系统配置
        defaults = {
            "amap_api_key": settings.amap_api_key,
            "default_city": settings.default_city,
            "enable_playwright": "true" if settings.enable_playwright else "false",
        }
        for k, v in defaults.items():
            existing = db.fetchone("SELECT setting_key FROM system_settings WHERE setting_key = %s", (k,))
            if not existing:
                db.execute(
                    "INSERT INTO system_settings (setting_key, setting_value) VALUES (%s, %s)",
                    (k, v),
                )
