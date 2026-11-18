import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


@dataclass
class Settings:
    db_type: str = os.getenv("DB_TYPE", "mysql").lower()
    mysql_host: str = os.getenv("MYSQL_HOST", "127.0.0.1")
    mysql_port: int = int(os.getenv("MYSQL_PORT", "3306"))
    mysql_user: str = os.getenv("MYSQL_USER", "radar_user")
    mysql_password: str = os.getenv("MYSQL_PASSWORD", "radar_pass_change_me")
    mysql_database: str = os.getenv("MYSQL_DATABASE", "new_store_radar")
    sqlite_path: str = os.getenv("SQLITE_PATH", "radar_test.db")

    amap_api_key: str = os.getenv("AMAP_API_KEY", "")
    default_city: str = os.getenv("DEFAULT_CITY", "")
    enable_playwright: bool = os.getenv("ENABLE_PLAYWRIGHT", "true").lower() == "true"
    playwright_headless: bool = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() == "true"
    playwright_min_delay: float = float(os.getenv("PLAYWRIGHT_MIN_DELAY_SEC", "3"))
    playwright_max_delay: float = float(os.getenv("PLAYWRIGHT_MAX_DELAY_SEC", "6"))
    app_secret: str = os.getenv("APP_SECRET", "dev-secret-change-in-prod")


settings = Settings()
