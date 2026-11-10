"""The shipped rules, routes and dashboard, running in the real programs."""

import json
import secrets

import httpx
import pytest

from tests.integration.conftest import (
    MONITORING,
    TOOLS,
    Process,
    Stack,
    free_port,
    tool,
    wait_until,
)

pytestmark = pytest.mark.integration


def test_every_shipped_rule_evaluates_without_error(stack):
    stack.start_service("stable", "v1")
    stack.start_alertmanager()
    stack.start_prometheus()
    stack.load(canary=False)

    def groups():
        body = httpx.get(f"{stack.prometheus_url}/api/v1/rules").json()
        rules = [r for g in body["data"]["groups"] for r in g["rules"]]
        return rules if all(r.get("lastEvaluation", "0001")[:4] != "0001" for r in rules) else None

    rules = wait_until(groups, 15, "every rule to be evaluated")
    assert len(rules) == 22
    assert [r["name"] for r in rules if r["health"] != "ok"] == []


def test_a_real_outage_reaches_the_pager_and_the_tickets_stay_quiet(stack):
    stack.start_service("stable", "v1", demo_error_ratio=1.0)
    stack.start_alertmanager()
    stack.start_prometheus()
    stack.load(canary=False)

    wait_until(lambda: "ErrorBudgetBurnFast" in stack.webhook.names("pager"), 30, "the page")
    alert = next(
        a
        for a in stack.webhook.received["pager"]
        if a["labels"]["alertname"] == "ErrorBudgetBurnFast"
    )
    assert alert["status"] == "firing"
    assert alert["labels"]["job"] == "quote-api"
    assert "ErrorBudgetBurnSlow" not in stack.webhook.names("ticket")
    assert "ErrorBudgetBurnGradual" not in stack.webhook.names("ticket")


def test_a_healthy_service_pages_nobody_and_the_heartbeat_arrives(stack):
    stack.start_service("stable", "v1")
    stack.start_alertmanager()
    stack.start_prometheus()
    stack.load(canary=False)

    wait_until(lambda: "Watchdog" in stack.webhook.names("heartbeat"), 30, "the heartbeat")
    wait_until(lambda: stack.query("job:http_errors:ratio_rate5m"), 15, "the recording rule")
    assert stack.webhook.received["pager"] == []
    assert float(stack.query("job:http_errors:ratio_rate5m")[0]["value"][1]) == 0


def test_the_heartbeat_and_the_pager_stay_on_their_own_routes(stack):
    stack.start_service("stable", "v1", demo_error_ratio=1.0)
    stack.start_alertmanager()
    stack.start_prometheus()
    stack.load(canary=False)
    wait_until(lambda: stack.webhook.names("heartbeat"), 30, "the heartbeat")
    wait_until(lambda: stack.webhook.names("pager"), 30, "the page")
    assert stack.webhook.names("heartbeat") == {"Watchdog"}
    assert "Watchdog" not in stack.webhook.names("pager")


@pytest.fixture(scope="module")
def grafana(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("grafana-stack")
    stack = Stack(tmp)
    stack.start_service("stable", "v1")
    stack.start_service("canary", "v2")
    stack.start_alertmanager()
    stack.start_prometheus()
    load = stack.load()
    load.set_canary_weight(20)

    port = free_port()
    password = secrets.token_urlsafe(16)
    tool("grafana/bin/grafana")
    process = Process(
        [str(TOOLS / "grafana/bin/grafana"), "server", f"--homepath={TOOLS / 'grafana'}"],
        tmp / "grafana.log",
        env={
            "GF_SERVER_HTTP_ADDR": "127.0.0.1",
            "GF_SERVER_HTTP_PORT": str(port),
            "GF_PATHS_DATA": str(tmp / "grafana-data"),
            "GF_PATHS_LOGS": str(tmp / "grafana-logs"),
            "GF_PATHS_PLUGINS": str(tmp / "grafana-plugins"),
            "GF_PATHS_PROVISIONING": str(MONITORING / "grafana/provisioning"),
            "GF_SECURITY_ADMIN_PASSWORD": password,
            "GF_ANALYTICS_REPORTING_ENABLED": "false",
            "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
            "PROMETHEUS_URL": stack.prometheus_url,
            "GRAFANA_DASHBOARDS": str(MONITORING / "grafana/dashboards"),
        },
    )
    stack.processes.append(process)
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", auth=("admin", password), timeout=20)
    wait_until(lambda: client.get("/api/health").status_code == 200, 90, "grafana")
    wait_until(
        lambda: client.get("/api/dashboards/uid/release-watch").status_code == 200,
        30,
        "the dashboard to be provisioned",
    )
    # Long enough for rate() over the panels' interval to have two samples.
    wait_until(lambda: stack.query("job:http_errors:ratio_rate5m"), 20, "recording rules")
    yield client, stack
    client.close()
    stack.stop()
    wait_until(lambda: process.group_is_gone, 10, "grafana and its plugins to exit")


def panels() -> list[dict]:
    path = MONITORING / "grafana/dashboards/release-watch.json"
    return json.loads(path.read_text())["panels"]


def test_the_datasource_can_reach_prometheus(grafana):
    client, _ = grafana

    # Grafana registers its Prometheus plugin in the background after it starts serving.
    def healthy():
        response = client.get("/api/datasources/uid/prometheus/health")
        assert response.json().get("status") == "OK", response.text
        return True

    wait_until(healthy, 180, "the datasource to report healthy")


def test_the_dashboard_is_provisioned_with_every_panel(grafana):
    client, _ = grafana
    dashboard = client.get("/api/dashboards/uid/release-watch").json()["dashboard"]
    assert len(dashboard["panels"]) == len(panels()) == 8


@pytest.mark.parametrize("panel", panels(), ids=lambda p: p["title"])
def test_every_panel_query_returns_data_through_grafana(grafana, panel):
    client, _ = grafana
    queries = [
        {
            "refId": target["refId"],
            "datasource": {"uid": "prometheus", "type": "prometheus"},
            "expr": target["expr"],
            "instant": target.get("instant", False),
            "range": target.get("range", True),
            "intervalMs": 15000,
            "maxDataPoints": 200,
        }
        for target in panel["targets"]
    ]

    def result():
        response = client.post(
            "/api/ds/query", json={"queries": queries, "from": "now-5m", "to": "now"}
        )
        assert response.status_code == 200, response.text
        frames = response.json()["results"]
        ok = all(
            frame["data"]["values"] and frame["data"]["values"][-1]
            for ref in frames.values()
            for frame in ref["frames"]
        ) and all(ref["frames"] for ref in frames.values())
        return ok

    wait_until(result, 120, f"data for {panel['title']}")


def test_a_panel_with_no_data_says_so_instead_of_looking_healthy():
    for panel in panels():
        assert panel["fieldConfig"]["defaults"]["noValue"] == "No data", panel["title"]
