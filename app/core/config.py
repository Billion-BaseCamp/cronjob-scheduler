import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    # Database
    DATABASE_URL_ASYNC: str = os.getenv("DATABASE_URL_ASYNC", "")

    # Microsoft Graph mail (same env names as auth-service / TSM)
    AZURE_CLIENT_ID: str = os.getenv("AZURE_CLIENT_ID", "").strip()
    AZURE_CLIENT_SECRET: str = os.getenv("AZURE_CLIENT_SECRET", "").strip()
    AZURE_TENANT_ID: str = os.getenv("AZURE_TENANT_ID", "").strip()
    EMAIL_SENDER: str = os.getenv("EMAIL_SENDER", "").strip()

    BIRTHDAY_EMAIL_JOB_ENABLED: bool = os.getenv(
        "BIRTHDAY_EMAIL_JOB_ENABLED", "true"
    ).lower() in ("1", "true", "yes")

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()