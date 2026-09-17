import os
from dotenv import load_dotenv

load_dotenv()


def _env_flag(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes")


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


class Settings:
    # Database
    DATABASE_URL_ASYNC: str = os.getenv("DATABASE_URL_ASYNC", "")

    # Microsoft Graph mail (same env names as auth-service / TSM)
    AZURE_CLIENT_ID: str = os.getenv("AZURE_CLIENT_ID", "").strip()
    AZURE_CLIENT_SECRET: str = os.getenv("AZURE_CLIENT_SECRET", "").strip()
    AZURE_TENANT_ID: str = os.getenv("AZURE_TENANT_ID", "").strip()
    EMAIL_SENDER: str = os.getenv("EMAIL_SENDER", "").strip()

    FINANCIAL_YEAR_JOB_ENABLED: bool = _env_flag("FINANCIAL_YEAR_JOB_ENABLED")
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