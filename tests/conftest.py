import os

os.environ.setdefault(
    "DATABASE_URL_ASYNC",
    "postgresql+asyncpg://user:pass@127.0.0.1:5432/testdb",
)
os.environ["DATABASE_URL_SYNC"] = "sqlite:///:memory:"
