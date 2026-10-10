import os
from typing import Optional
from uuid import UUID

from dotenv import load_dotenv

load_dotenv()
# nucleus.db.database creates a sync engine at import from DATABASE_URL_SYNC.
# This process only uses DATABASE_URL_ASYNC (asyncpg). Always overwrite so a
# copied tax-engine .env (postgresql+psycopg2) cannot pull in psycopg2.
os.environ["DATABASE_URL_SYNC"] = "sqlite:///:memory:"


def _env_flag(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")


def _env_int(name: str, default: int) -> int:
    """Int from env, falling back to the default on a blank or malformed value.

    A blank env var is common in deploy templates; ``int("")`` would crash the
    whole scheduler at import.
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: str) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except ValueError:
        return float(default)


def parse_uuid_list(raw: Optional[str]) -> Optional[tuple[UUID, ...]]:
    """None when blank. Malformed entries are dropped, never widening to "all".

    A value that is set but has no valid UUID gives an empty tuple, so a typo
    on staging checks nobody instead of every client.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    ids: list[UUID] = []
    for part in raw.split(","):
        try:
            ids.append(UUID(part.strip()))
        except ValueError:
            continue
    return tuple(dict.fromkeys(ids))


class Settings:
    APP_ENV: str = os.getenv("APP_ENV", "staging")
    APP_NAME: str = os.getenv("APP_NAME", "BBC Ops Worker")
    DEBUG: bool = _env_flag("DEBUG", "false")

    DATABASE_URL_ASYNC: str = os.getenv("DATABASE_URL_ASYNC", "")

    AZURE_CLIENT_ID: str = os.getenv("AZURE_CLIENT_ID", "").strip()
    AZURE_CLIENT_SECRET: str = os.getenv("AZURE_CLIENT_SECRET", "").strip()
    AZURE_TENANT_ID: str = os.getenv("AZURE_TENANT_ID", "").strip()
    EMAIL_SENDER: str = os.getenv("EMAIL_SENDER", "").strip()

    FINANCIAL_YEAR_JOB_ENABLED: bool = _env_flag("FINANCIAL_YEAR_JOB_ENABLED")
    # Off by default so a portal deploy does not fire FY for every client.
    FINANCIAL_YEAR_RUN_ON_STARTUP: bool = _env_flag(
        "FINANCIAL_YEAR_RUN_ON_STARTUP", "false"
    )
    QUARTER_TRANSITION_JOB_ENABLED: bool = _env_flag("QUARTER_TRANSITION_JOB_ENABLED")
    BIRTHDAY_EMAIL_JOB_ENABLED: bool = _env_flag("BIRTHDAY_EMAIL_JOB_ENABLED")

    PORTAL_WORKER_ENABLED: bool = _env_flag("PORTAL_WORKER_ENABLED", "false")
    DOCUMENT_PASSWORD_ENCRYPTION_KEY: str = os.getenv(
        "DOCUMENT_PASSWORD_ENCRYPTION_KEY", ""
    )
    PORTAL_AUTOMATION_DRY_RUN: bool = _env_flag("PORTAL_AUTOMATION_DRY_RUN", "true")
    PORTAL_AUTOMATION_DRY_RUN_INVALID_PASSWORD: bool = _env_flag(
        "PORTAL_AUTOMATION_DRY_RUN_INVALID_PASSWORD", "false"
    )
    PORTAL_LOGIN_URL: str = os.getenv(
        "PORTAL_LOGIN_URL",
        "https://eportal.incometax.gov.in/iec/foservices/#/login",
    )
    PORTAL_HOME_URL: str = os.getenv(
        "PORTAL_HOME_URL",
        "https://eportal.incometax.gov.in/iec/foservices/#/pre-login/homepage",
    )
    PLAYWRIGHT_HEADLESS: bool = _env_flag("PLAYWRIGHT_HEADLESS", "true")
    WORKER_POLL_SECONDS: float = _env_float("WORKER_POLL_SECONDS", "2")
    WORKER_CONCURRENCY: int = min(8, max(1, _env_int("WORKER_CONCURRENCY", 2)))
    WORKER_HEARTBEAT_SECONDS: float = _env_float("WORKER_HEARTBEAT_SECONDS", "30")
    MAX_ATTEMPTS: int = _env_int("MAX_ATTEMPTS", 3)
    PORTAL_AUTOMATION_TIMEOUT_MINUTES: int = _env_int(
        "PORTAL_AUTOMATION_TIMEOUT_MINUTES", 15
    )
    PORTAL_AUTOMATION_SWEEP_INTERVAL_SECONDS: int = _env_int(
        "PORTAL_AUTOMATION_SWEEP_INTERVAL_SECONDS", 60
    )
    # Scheduled notice runs. Off by default; turn on after a monthly run started
    # by hand has built the first weekly list.
    E_PROCEEDINGS_MONTHLY_CRON_ENABLED: bool = _env_flag(
        "E_PROCEEDINGS_MONTHLY_CRON_ENABLED", "false"
    )
    E_PROCEEDINGS_WEEKLY_CRON_ENABLED: bool = _env_flag(
        "E_PROCEEDINGS_WEEKLY_CRON_ENABLED", "false"
    )
    OUTSTANDING_DEMAND_CRON_ENABLED: bool = _env_flag(
        "OUTSTANDING_DEMAND_CRON_ENABLED", "false"
    )
    # Completes running scheduled batches. Needed for runs started by hand too.
    NOTICE_BATCH_FINALIZER_ENABLED: bool = _env_flag(
        "NOTICE_BATCH_FINALIZER_ENABLED", "true"
    )
    NOTICE_BATCH_FINALIZER_MINUTES: int = max(
        1, _env_int("NOTICE_BATCH_FINALIZER_MINUTES", 5)
    )
    NOTICE_BATCH_DEADLINE_HOURS: int = max(
        1, _env_int("NOTICE_BATCH_DEADLINE_HOURS", 30)
    )
    E_PROCEEDINGS_MONTHLY_MIN_SUCCESS_RATIO: float = _env_float(
        "E_PROCEEDINGS_MONTHLY_MIN_SUCCESS_RATIO", "0.8"
    )
    # Staging only: limit scheduled notice runs to these client ids. Keep empty
    # in production; a limited monthly run would become the weekly list.
    NOTICE_CRON_CLIENT_IDS: Optional[tuple[UUID, ...]] = parse_uuid_list(
        os.getenv("NOTICE_CRON_CLIENT_IDS")
    )

    S3_BUCKET_NAME: str = os.getenv("S3_BUCKET_NAME", "")
    S3_REGION: str = os.getenv("S3_REGION", os.getenv("AWS_REGION", "ap-south-1"))

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
