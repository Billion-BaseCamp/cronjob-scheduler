import pytest

pytest.importorskip("playwright")
pytest.importorskip("nucleus")
pytest.importorskip("boto3")
pytest.importorskip("cryptography")


def test_check_itr_workflow_registered() -> None:
    from app.portal.workflows.registry import get_runner

    assert get_runner("CHECK_ITR_VERIFICATION") is not None
    assert get_runner("unknown") is None
