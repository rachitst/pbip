"""TASK-069/103: dashboards and alert rules are provisioned from the repository and stay consistent with
the metrics the API actually exports (metric names are a public contract, core/metrics.py)."""

import json
import re
from pathlib import Path

import pytest
import yaml
from pbip_api.core import metrics
from prometheus_client.metrics import MetricWrapperBase

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
DASHBOARDS = sorted((ROOT / "infra" / "grafana" / "dashboards").glob("*.json"))
ALERTS = ROOT / "infra" / "prometheus" / "alerts.yml"
PROMETHEUS = ROOT / "infra" / "prometheus" / "prometheus.yml"
# Families owned by this application; anything else in an expression comes from an exporter.
OWN_PREFIXES = ("http_request", "fraud_", "transfers_", "idempotent_", "login_", "unhandled_")
SUFFIXES = ("_bucket", "_count", "_sum", "_total", "_created")


def _exported_names() -> set[str]:
    names = set()
    for value in vars(metrics).values():
        if isinstance(value, MetricWrapperBase):
            names.update(family.name for family in value.describe())
    return names


def _own_metric_names(expr: str) -> set[str]:
    found = set()
    for token in re.findall(r"[a-z_][a-z0-9_]*", expr):
        if token.startswith(OWN_PREFIXES):
            for suffix in SUFFIXES:
                if token.endswith(suffix):
                    token = token[: -len(suffix)]
                    break
            found.add(token)
    return found


def _dashboard_exprs() -> list[tuple[str, str, str]]:
    out = []
    for path in DASHBOARDS:
        board = json.loads(path.read_text(encoding="utf-8"))
        for panel in board["panels"]:
            default = (panel.get("datasource") or {}).get("uid", "prometheus")  # Grafana's default
            for target in panel.get("targets", []):
                uid = (target.get("datasource") or {}).get("uid", default)
                out.append((path.name, uid, target["expr"]))
    return out


def test_dashboards_have_unique_uids_and_cover_the_required_views() -> None:
    uids = [json.loads(p.read_text(encoding="utf-8"))["uid"] for p in DASHBOARDS]
    assert len(uids) == len(set(uids))
    assert {"pbip-api", "pbip-fraud", "pbip-platform", "pbip-availability"} <= set(uids)


def test_every_panel_uses_a_provisioned_datasource() -> None:
    provisioned = yaml.safe_load(
        (ROOT / "infra/grafana/provisioning/datasources/datasources.yml").read_text(
            encoding="utf-8"
        )
    )
    uids = {d["uid"] for d in provisioned["datasources"]}
    for name, uid, _ in _dashboard_exprs():
        assert uid in uids, name


def test_dashboards_and_alerts_reference_only_metrics_the_api_exports() -> None:
    exported = _exported_names()
    exprs = [expr for _, uid, expr in _dashboard_exprs() if uid == "prometheus"]
    rules = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
    exprs += [rule["expr"] for group in rules["groups"] for rule in group["rules"]]
    used = set().union(*(_own_metric_names(e) for e in exprs))
    assert used, "no application metrics referenced"
    assert used <= exported, f"unknown metrics: {sorted(used - exported)}"


def test_latency_is_shown_per_route_template_at_p50_p95_p99() -> None:
    api = next(p for p in DASHBOARDS if p.name == "pbip-api.json").read_text(encoding="utf-8")
    for quantile in ("0.5", "0.95", "0.99"):
        assert f"histogram_quantile({quantile}, sum by (le, route)" in api


def test_fraud_dashboard_splits_all_four_decisions() -> None:
    fraud = next(p for p in DASHBOARDS if p.name == "pbip-fraud.json").read_text(encoding="utf-8")
    assert "sum by (decision) (rate(fraud_decisions_total" in fraud


def test_availability_alert_and_monthly_figure_exist() -> None:
    rules = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
    by_name = {r.get("alert") or r.get("record"): r for g in rules["groups"] for r in g["rules"]}
    assert "< 0.99" in by_name["AvailabilityBelow99"]["expr"]
    assert "[30d]" in by_name["pbip:availability:ratio_30d"]["expr"]
    scrape = yaml.safe_load(PROMETHEUS.read_text(encoding="utf-8"))
    jobs = {job["job_name"] for job in scrape["scrape_configs"]}
    assert {"pbip-api", "blackbox", "celery", "postgres", "redis"} <= jobs
