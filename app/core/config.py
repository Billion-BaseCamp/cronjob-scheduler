import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    # Database
    DATABASE_URL_ASYNC: str = os.getenv("DATABASE_URL_ASYNC", "")

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()