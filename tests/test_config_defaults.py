from app.core.config import _env_flag, settings


def test_worker_concurrency_is_clamped() -> None:
    assert 1 <= settings.WORKER_CONCURRENCY <= 8


def test_run_on_startup_helper_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv("FINANCIAL_YEAR_RUN_ON_STARTUP", raising=False)
    assert _env_flag("FINANCIAL_YEAR_RUN_ON_STARTUP", "false") is False
