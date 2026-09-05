import os
from dotenv import load_dotenv

load_dotenv()


def _env_flag(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes")


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

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()