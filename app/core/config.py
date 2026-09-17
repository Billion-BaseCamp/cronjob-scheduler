import os
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

    # --- Account Aggregator worker (Finvu/Finsense) ---
    # Default OFF: the worker ships dark and is enabled per environment.
    AA_WORKER_ENABLED: bool = _env_flag("AA_WORKER_ENABLED", "false")
    AA_SWEEPER_ENABLED: bool = _env_flag("AA_SWEEPER_ENABLED", "false")

    AA_WORKER_POLL_INTERVAL_SECONDS: int = _env_int("AA_WORKER_POLL_INTERVAL_SECONDS", 10)
    AA_SWEEP_INTERVAL_SECONDS: int = _env_int("AA_SWEEP_INTERVAL_SECONDS", 60)
    AA_STALE_JOB_TIMEOUT_MINUTES: int = _env_int("AA_STALE_JOB_TIMEOUT_MINUTES", 30)
    AA_FETCH_MAX_ATTEMPTS: int = _env_int("AA_FETCH_MAX_ATTEMPTS", 5)
    AA_FETCH_BACKOFF_BASE_SECONDS: int = _env_int("AA_FETCH_BACKOFF_BASE_SECONDS", 120)
    AA_CONSENT_DEFAULT_MONTHS: int = _env_int("AA_CONSENT_DEFAULT_MONTHS", 24)

    # Finsense — same values as aa-backend; the two share the Redis token cache.
    FINSENSE_BASE_URL: str = os.getenv(
        "FINSENSE_BASE_URL", "https://quantmutual.fiu.finfactor.in/finsense/API/V2"
    ).rstrip("/")
    FINSENSE_CHANNEL_ID: str = os.getenv("FINSENSE_CHANNEL_ID", "finsense").strip()
    FINSENSE_USER_ID: str = os.getenv("FINSENSE_USER_ID", "").strip()
    FINSENSE_PASSWORD: str = os.getenv("FINSENSE_PASSWORD", "").strip()
    FINSENSE_AA_ID: str = os.getenv("FINSENSE_AA_ID", "cookiejar-aa@finvu.in").strip()
    FINSENSE_TEMPLATE_BANK: str = os.getenv(
        "FINSENSE_TEMPLATE_BANK", "BANK_STATEMENT_others"
    ).strip()
    FINSENSE_REDIRECT_URL: str = os.getenv(
        "FINSENSE_REDIRECT_URL", "https://aa.billionbasecamp.com/aa/return"
    ).strip()
    FINSENSE_TIMEOUT_SECONDS: int = _env_int("FINSENSE_TIMEOUT_SECONDS", 45)

    AA_REDIS_URL: str = os.getenv("AA_REDIS_URL", "redis://localhost:6379/3").strip()
    AA_TOKEN_FALLBACK_TTL_SECONDS: int = _env_int("AA_TOKEN_FALLBACK_TTL_SECONDS", 82800)
    AA_TOKEN_REFRESH_MARGIN_SECONDS: int = _env_int("AA_TOKEN_REFRESH_MARGIN_SECONDS", 300)
    AA_TOKEN_LOCK_TIMEOUT_SECONDS: int = _env_int("AA_TOKEN_LOCK_TIMEOUT_SECONDS", 30)

    # Separate from DOCUMENT_PASSWORD_ENCRYPTION_KEY on purpose: portal
    # passwords and bank statements have different blast radii.
    AA_ENCRYPTION_KEY: str = os.getenv("AA_ENCRYPTION_KEY", "").strip()

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
    S3_BUCKET_NAME: str = os.getenv("S3_BUCKET_NAME", "")
    S3_REGION: str = os.getenv("S3_REGION", os.getenv("AWS_REGION", "ap-south-1"))

    class Config:
        env_file = ".env"
        case_sensitive = True

    def require_finsense(self) -> None:
        """Fail loudly when vendor credentials are missing.

        An empty password produces a vendor 401 that looks like a permissions
        problem rather than a config problem, which is a slow thing to debug.
        """
        missing = [
            name
            for name in ("FINSENSE_USER_ID", "FINSENSE_PASSWORD")
            if not getattr(self, name, "")
        ]
        if missing:
            raise RuntimeError("Finsense credentials missing: " + ", ".join(missing))

    def require_aa_encryption_key(self) -> None:
        if not self.AA_ENCRYPTION_KEY:
            raise RuntimeError(
                "AA_ENCRYPTION_KEY is not set — refusing to store financial data "
                "unencrypted."
            )


settings = Settings()
