"""Facts that span files, which neither Terraform's tests nor promtool can see."""

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SERVICE = (ROOT / "infra/modules/service/main.tf").read_text()
MONITORING = (ROOT / "infra/modules/monitoring/main.tf").read_text()
TEMPLATE = ROOT / "infra/modules/monitoring/bootstrap.sh.tftpl"
PROMETHEUS = yaml.safe_load((ROOT / "monitoring/prometheus/prometheus.yml").read_text())


def block(source: str, header: str) -> str:
    start = source.index(header)
    opening = source.index("{", start)
    depth = 0
    for index, char in enumerate(source[opening:], start=opening):
        depth += {"{": 1, "}": -1}.get(char, 0)
        if depth == 0:
            return source[start : index + 1]
    raise AssertionError(f"unterminated block {header}")


def code(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


def test_a_terraform_apply_cannot_reset_the_weights_mid_release():
    assert "ignore_changes = [default_action]" in code(
        block(SERVICE, 'resource "aws_lb_listener" "https"')
    )


def test_a_release_cannot_shrink_a_scaled_up_stable_service():
    assert "ignore_changes = [desired_count]" in code(
        block(SERVICE, 'resource "aws_ecs_service" "stable"')
    )


def test_the_canary_count_stays_under_terraform_control():
    assert "ignore_changes" not in block(SERVICE, 'resource "aws_ecs_service" "canary"')


def test_the_metrics_disk_cannot_be_destroyed_by_an_apply():
    assert "prevent_destroy = true" in code(block(MONITORING, 'resource "aws_ebs_volume" "data"'))


def test_prometheus_looks_for_exactly_the_names_terraform_registers():
    job = next(j for j in PROMETHEUS["scrape_configs"] if j["job_name"] == "quote-api")
    sd = job["dns_sd_configs"][0]
    assert sorted(sd["names"]) == ["canary.quote-api.internal", "stable.quote-api.internal"]
    assert sd["type"] == "A"
    assert sd["port"] == int(re.search(r"port = (\d+)", SERVICE).group(1))
    assert 'name = "${var.name}.internal"' in SERVICE


def test_the_track_label_comes_from_the_discovery_name():
    job = next(j for j in PROMETHEUS["scrape_configs"] if j["job_name"] == "quote-api")
    rule = job["relabel_configs"][0]
    assert rule["target_label"] == "track"
    pattern = re.compile(rule["regex"])
    for track in ("stable", "canary"):
        assert pattern.fullmatch(f"{track}.quote-api.internal").group(1) == track
    assert pattern.fullmatch("other.quote-api.internal") is None


def test_scrapes_are_frequent_enough_for_the_gate_to_trust_them():
    from gate.decision import Policy

    seconds = int(PROMETHEUS["global"]["scrape_interval"].rstrip("s"))
    assert Policy().max_scrape_age_seconds >= 3 * seconds


def test_retention_covers_the_longest_rule_window():
    rendered = render()
    days = int(re.search(r"retention\.time=(\d+)d", rendered).group(1))
    rules = (ROOT / "monitoring/prometheus/rules/slo.yml").read_text()
    longest = max(int(d) for d in re.findall(r"\[(\d+)d\]", rules))
    assert days > longest


def render() -> str:
    values = {
        "region": "eu-central-1",
        "name": "quote-api",
        "volume_id": "vol-0abc",
        "prometheus_image": "1.dkr.ecr.eu-central-1.amazonaws.com/p:m1",
        "alertmanager_image": "1.dkr.ecr.eu-central-1.amazonaws.com/a:m1",
        "grafana_image": "1.dkr.ecr.eu-central-1.amazonaws.com/g:m1",
    }
    text = re.sub(r"(?<!\$)\$\{(\w+)\}", lambda m: values[m.group(1)], TEMPLATE.read_text())
    return text.replace("$${", "${")


class Host:
    """Runs functions from the real bootstrap script with the system commands faked."""

    def __init__(self, tmp: Path, blkid_exit: int = 0, aws_exit: int = 0):
        self.tmp = tmp
        self.log = tmp / "calls.log"
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.data = tmp / "data"
        self.secrets = tmp / "secrets"
        self.script = tmp / "bootstrap.sh"
        self.script.write_text(render())
        self._fake("blkid", blkid_exit)
        self._fake("mkfs.ext4")
        self._fake("mount")
        self._fake("chown")
        self._fake("aws", aws_exit, 'echo "s3cret"')

    def _fake(self, name: str, code: int = 0, body: str = "") -> None:
        path = self.bin / name
        path.write_text(f'#!/bin/sh\necho "{name} $*" >> "{self.log}"\n{body}\nexit {code}\n')
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def run(self, function: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "BOOTSTRAP_TEST": "1",
            "DATA_DIR": str(self.data),
            "SECRETS_DIR": str(self.secrets),
        }
        return subprocess.run(
            ["bash", "-c", f'source "{self.script}" && {function}'],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    @property
    def calls(self) -> str:
        return self.log.read_text() if self.log.exists() else ""


def test_every_template_value_is_filled_in():
    assert re.search(r"(?<!\$)\$\{(region|name|volume_id|\w+_image)\}", render()) is None


def test_the_disk_is_found_by_the_name_the_kernel_gives_it():
    assert 'nvme-Amazon_Elastic_Block_Store_$(echo "vol-0abc" | tr -d -)' in render()


def test_a_blank_disk_is_formatted_then_mounted(tmp_path):
    host = Host(tmp_path, blkid_exit=2)
    result = host.run("prepare_disk")
    assert result.returncode == 0, result.stderr
    assert "mkfs.ext4" in host.calls
    assert host.calls.index("mkfs.ext4") < host.calls.index("mount")


def test_a_disk_with_metrics_on_it_is_never_formatted(tmp_path):
    host = Host(tmp_path, blkid_exit=0)
    assert host.run("prepare_disk").returncode == 0
    assert "mkfs.ext4" not in host.calls
    assert "mount" in host.calls


def test_each_program_owns_its_own_data_folder(tmp_path):
    host = Host(tmp_path)
    host.run("prepare_disk")
    assert f"chown -R 65534:65534 {host.data}/prometheus {host.data}/alertmanager" in host.calls
    assert f"chown -R 472:472 {host.data}/grafana" in host.calls


def test_secrets_land_readable_only_by_their_owner(tmp_path):
    host = Host(tmp_path)
    result = host.run("read_secrets")
    assert result.returncode == 0, result.stderr
    target = host.secrets / "pager_url"
    assert target.read_text().strip() == "s3cret"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert "chown 472:472" in host.calls


@pytest.mark.parametrize("secret", ["pager_url", "ticket_url", "heartbeat_url", "grafana_password"])
def test_every_secret_is_fetched_under_the_service_prefix(tmp_path, secret):
    host = Host(tmp_path)
    host.run("read_secrets")
    assert f"--secret-id quote-api/{secret}" in host.calls


def test_a_secret_that_cannot_be_read_stops_the_boot(tmp_path):
    host = Host(tmp_path, aws_exit=1)
    assert host.run("set -e; read_secrets").returncode != 0
