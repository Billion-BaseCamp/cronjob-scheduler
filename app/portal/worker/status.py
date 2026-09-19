"""Poller task status for /health. No DB imports."""


def poller_status(task) -> str:
    if task is None:
        return "not_started"
    if not task.done():
        return "running"
    return "stopped"
