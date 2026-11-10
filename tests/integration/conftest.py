"""Runs the real Prometheus, Alertmanager, Grafana and service as local processes.

Every config is derived from the file that ships, with only addresses and intervals
changed, so a mistake in a shipped rule or dashboard shows up here.
"""

import json
import os
import random
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TOOLS = Path(os.environ.get("TOOLS_DIR", ROOT / ".tools"))
MONITORING = ROOT / "monitoring"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(check, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except Exception as exc:  # noqa: BLE001 - still starting
            last = exc
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}: {last}")


def tool(path: str) -> str:
    binary = TOOLS / path
    if not binary.exists():
        pytest.fail(f"{binary} is missing. Run scripts/fetch-tools.sh first.")
    return str(binary)


class Process:
    """A program in its own process group, because Grafana starts plugin children."""

    def __init__(self, args, log: Path, env=None, cwd=None):
        self.log = log
        self.handle = subprocess.Popen(
            args,
            stdout=log.open("w"),
            stderr=subprocess.STDOUT,
            env={**os.environ, **(env or {})},
            cwd=cwd,
            start_new_session=True,
        )
        self.group = self.handle.pid

    def _signal_group(self, number: int) -> bool:
        try:
            os.killpg(self.group, number)
        except ProcessLookupError:
            return False
        return True

    def stop(self):
        self._signal_group(signal.SIGTERM)
        try:
            self.handle.wait(10)
        except subprocess.TimeoutExpired:
            self.handle.kill()
            self.handle.wait(5)
        self._signal_group(signal.SIGKILL)

    @property
    def group_is_gone(self) -> bool:
        return not self._signal_group(0)


class Webhook:
    """Stands in for the pager, the ticket queue and the heartbeat service."""

    def __init__(self):
        self.received: dict[str, list[dict]] = {"pager": [], "ticket": [], "heartbeat": []}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.received[self.path.strip("/")].extend(body["alerts"])
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def names(self, receiver: str) -> set[str]:
        return {alert["labels"]["alertname"] for alert in list(self.received[receiver])}

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class Load:
    """Plays the load balancer: splits requests between the tracks by weight."""

    def __init__(self, stable_url: str, canary_url: str | None, threads: int = 8):
        self.stable_url, self.canary_url = stable_url, canary_url
        self.weight = 0
        self.weights: list[int] = []
        self.sent = {"stable": 0, "canary": 0}
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._run, daemon=True) for _ in range(threads)]
        for thread in self._threads:
            thread.start()

    def set_canary_weight(self, percent: int) -> None:
        self.weight = percent
        self.weights.append(percent)

    def _run(self):
        rng = random.Random()
        with httpx.Client(timeout=2) as client:
            while not self._stop.is_set():
                canary = self.canary_url and rng.randrange(100) < self.weight
                track = "canary" if canary else "stable"
                url = self.canary_url if canary else self.stable_url
                try:
                    client.get(f"{url}/api/quote", params={"units": 3})
                    self.sent[track] += 1
                except httpx.HTTPError:
                    time.sleep(0.01)

    def stop(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(5)


class Stack:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.processes: list[Process] = []
        self.webhook = Webhook()
        self.loads: list[Load] = []
        self.targets: dict[str, int] = {}
        self.prometheus_port = free_port()
        self.alertmanager_port = free_port()
        self.prometheus_url = f"http://127.0.0.1:{self.prometheus_port}"
        self.prometheus: Process | None = None

    def start_service(self, track: str, version: str, **env) -> Process:
        port = self.targets.setdefault(track, free_port())
        process = Process(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.asgi:app",
                "--port",
                str(port),
                "--log-level",
                "warning",
                "--no-access-log",
            ],
            self.tmp / f"{track}.log",
            env={"APP_VERSION": version, **{k.upper(): str(v) for k, v in env.items()}},
            cwd=ROOT,
        )
        self.processes.append(process)
        wait_until(
            lambda: httpx.get(f"http://127.0.0.1:{port}/healthz").status_code == 200,
            15,
            f"{track} service",
        )
        return process

    def url(self, track: str) -> str:
        return f"http://127.0.0.1:{self.targets[track]}"

    def start_alertmanager(self):
        config = yaml.safe_load((MONITORING / "alertmanager/alertmanager.yml").read_text())
        for receiver in config["receivers"]:
            hook = receiver["webhook_configs"][0]
            del hook["url_file"]
            hook["url"] = f"http://127.0.0.1:{self.webhook.port}/{receiver['name']}"
        config["route"].update(group_wait="1s", group_interval="2s")
        for route in config["route"]["routes"]:
            route.update(group_wait="1s", group_interval="2s")
        path = self.tmp / "alertmanager.yml"
        path.write_text(yaml.safe_dump(config))
        process = Process(
            [
                tool("alertmanager/alertmanager"),
                f"--config.file={path}",
                f"--web.listen-address=127.0.0.1:{self.alertmanager_port}",
                f"--storage.path={self.tmp / 'am'}",
                "--cluster.listen-address=",
            ],
            self.tmp / "alertmanager.log",
        )
        self.processes.append(process)
        wait_until(
            lambda: (
                httpx.get(f"http://127.0.0.1:{self.alertmanager_port}/-/ready").status_code == 200
            ),
            15,
            "alertmanager",
        )

    def start_prometheus(self):
        config = yaml.safe_load((MONITORING / "prometheus/prometheus.yml").read_text())
        config["global"] = {"scrape_interval": "1s", "evaluation_interval": "1s"}
        config["rule_files"] = [str(MONITORING / "prometheus/rules/*.yml")]
        config["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"] = [
            f"127.0.0.1:{self.alertmanager_port}"
        ]
        job = next(j for j in config["scrape_configs"] if j["job_name"] == "quote-api")
        for key in ("dns_sd_configs", "relabel_configs"):
            del job[key]
        job["static_configs"] = [
            {"targets": [f"127.0.0.1:{port}"], "labels": {"track": track}}
            for track, port in self.targets.items()
        ]
        config["scrape_configs"] = [job]
        path = self.tmp / "prometheus.yml"
        path.write_text(yaml.safe_dump(config))
        self.prometheus = Process(
            [
                tool("prometheus/prometheus"),
                f"--config.file={path}",
                f"--web.listen-address=127.0.0.1:{self.prometheus_port}",
                f"--storage.tsdb.path={self.tmp / 'tsdb'}",
            ],
            self.tmp / "prometheus.log",
        )
        self.processes.append(self.prometheus)
        wait_until(
            lambda: httpx.get(f"{self.prometheus_url}/-/ready").status_code == 200, 20, "prometheus"
        )

    def query(self, promql: str) -> list:
        response = httpx.get(f"{self.prometheus_url}/api/v1/query", params={"query": promql})
        return response.json()["data"]["result"]

    def load(self, canary: bool = True) -> Load:
        load = Load(self.url("stable"), self.url("canary") if canary else None)
        self.loads.append(load)
        return load

    def stop(self):
        for load in self.loads:
            load.stop()
        for process in reversed(self.processes):
            process.stop()
        self.webhook.stop()


@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path)
    yield s
    s.stop()
