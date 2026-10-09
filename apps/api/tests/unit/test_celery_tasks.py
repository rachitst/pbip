import pytest
from pbip_api.worker.tasks import ping

pytestmark = pytest.mark.unit


def test_ping_task_returns_pong() -> None:
    assert ping() == "pong"
