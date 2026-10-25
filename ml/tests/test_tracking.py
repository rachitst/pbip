"""TASK-011: MLflow client helper - server resolution, local fallback, dummy run (no network)."""

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import mlflow
import pytest
from mlflow.tracking import MlflowClient
from pbip_ml.tracking import (
    DEFAULT_TRACKING_URI,
    TRACKING_URI_ENV,
    configure_tracking,
    ensure_experiment,
    resolve_tracking_target,
    server_is_reachable,
)
from pbip_ml.tracking_smoke import log_dummy_run, main

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_global_tracking_uri(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(TRACKING_URI_ENV, raising=False)
    previous = mlflow.get_tracking_uri()
    yield
    mlflow.set_tracking_uri(previous)


def _stub_server(status: int) -> tuple[HTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # http.server API
            self.send_response(status if self.path == "/health" else 404)
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


@pytest.mark.parametrize(("status", "expected"), [(200, True), (503, False)])
def test_health_probe_reads_the_server_status(status: int, expected: bool) -> None:
    server, url = _stub_server(status)
    try:
        assert server_is_reachable(url) is expected
    finally:
        server.shutdown()


def test_health_probe_is_false_for_refused_or_non_http_targets() -> None:
    assert server_is_reachable("http://127.0.0.1:1", timeout=0.5) is False
    assert server_is_reachable("sqlite:///x.db") is False


def test_reachable_server_is_used(tmp_path: Path) -> None:
    target = resolve_tracking_target("http://mlflow:5000", tmp_path, probe=lambda _uri: True)
    assert (target.uri, target.is_remote, target.artifact_location) == (
        "http://mlflow:5000", True, None,
    )  # fmt: skip


def test_unreachable_server_falls_back_to_local_sqlite(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    target = resolve_tracking_target(None, tmp_path, probe=lambda _uri: False)
    assert target.is_remote is False
    assert target.uri == f"sqlite:///{tmp_path.resolve() / 'mlflow.db'}"
    assert target.artifact_location == (tmp_path.resolve() / "artifacts").as_uri()
    assert "using local tracking store" in caplog.text  # the fallback is never silent


def test_uri_resolution_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    probed: list[str] = []

    def probe(uri: str) -> bool:
        probed.append(uri)
        return True

    resolve_tracking_target(None, tmp_path, probe)
    monkeypatch.setenv(TRACKING_URI_ENV, "http://from-env:5000")
    resolve_tracking_target(None, tmp_path, probe)
    resolve_tracking_target("http://explicit:5000", tmp_path, probe)
    assert probed == [DEFAULT_TRACKING_URI, "http://from-env:5000", "http://explicit:5000"]
    explicit_local = resolve_tracking_target("sqlite:///elsewhere.db", tmp_path, probe)
    assert explicit_local.uri == "sqlite:///elsewhere.db" and not explicit_local.is_remote


def test_dummy_run_is_logged_and_read_back_on_the_fallback(tmp_path: Path) -> None:
    target = configure_tracking("http://unreachable.invalid:5000", tmp_path, lambda _uri: False)
    result = log_dummy_run(target)
    run = MlflowClient(tracking_uri=target.uri).get_run(result.run_id)
    assert run.data.params["model"] == "dummy-baseline"
    assert run.data.metrics["pr_auc"] == pytest.approx(0.71)  # latest step
    assert run.data.tags["task"] == "TASK-011"
    assert result.artifacts == ("summary.json",)
    assert (tmp_path / "artifacts").is_dir()
    assert (
        ensure_experiment("pbip-smoke", target) == run.info.experiment_id
    )  # reused, not duplicated


def test_smoke_cli_can_require_a_server(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["--tracking-uri", "http://127.0.0.1:1", "--local-dir", str(tmp_path), "--require-server"]
    )
    assert code == 1
    assert "not reachable" in capsys.readouterr().err
