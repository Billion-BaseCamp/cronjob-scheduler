from app.portal.worker.status import poller_status


def test_poller_status() -> None:
    assert poller_status(None) == "not_started"

    class _Running:
        def done(self) -> bool:
            return False

    class _Stopped:
        def done(self) -> bool:
            return True

    assert poller_status(_Running()) == "running"
    assert poller_status(_Stopped()) == "stopped"
