#!/usr/bin/env bash
# Builds every image and starts it the way the monitoring host and ECS start it.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
secrets="$(mktemp -d)"
network="smoke-$$"

cleanup() {
  docker rm -f smoke-app smoke-prometheus smoke-alertmanager smoke-grafana >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  rm -rf "$secrets"
}
trap cleanup EXIT

wait_for() { # description, command...
  local what="$1"
  shift
  for _ in $(seq 1 60); do
    if "$@" >/dev/null 2>&1; then
      echo "ok: $what"
      return 0
    fi
    sleep 2
  done
  echo "failed: $what" >&2
  docker ps -a >&2
  exit 1
}

docker build -q -t release-watch/app -f app/Dockerfile .
for image in prometheus alertmanager grafana; do
  docker build -q -t "release-watch/$image" -f "monitoring/Dockerfile.$image" monitoring
done

docker network create "$network" >/dev/null

# Read only and not root, as the ECS task definition runs it.
docker run -d --name smoke-app --network "$network" --read-only --user 10001 \
  -e APP_VERSION=smoke -p 127.0.0.1:18000:8000 release-watch/app >/dev/null
wait_for "service answers" curl -fs http://127.0.0.1:18000/healthz
curl -fs "http://127.0.0.1:18000/api/quote?units=2" | grep -q '"total_cents":2500'
echo "ok: service prices a quote"

docker run --rm --entrypoint promtool release-watch/prometheus check config /etc/prometheus/prometheus.yml
docker run --rm --entrypoint amtool release-watch/alertmanager check-config /etc/alertmanager/alertmanager.yml

docker run -d --name smoke-prometheus --network "$network" --network-alias prometheus \
  -p 127.0.0.1:19090:9090 release-watch/prometheus \
  --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=/prometheus \
  --storage.tsdb.retention.time=35d >/dev/null
wait_for "prometheus is ready" curl -fs http://127.0.0.1:19090/-/ready
curl -fs http://127.0.0.1:19090/api/v1/rules | grep -q ErrorBudgetBurnFast
echo "ok: prometheus loaded the rules"

for name in pager_url ticket_url heartbeat_url; do
  echo "http://127.0.0.1:9/$name" >"$secrets/$name"
done
echo "smoke-password" >"$secrets/grafana_password"
chmod 644 "$secrets"/*
chmod 755 "$secrets"

docker run -d --name smoke-alertmanager --network "$network" -v "$secrets:/run/secrets:ro" \
  -p 127.0.0.1:19093:9093 release-watch/alertmanager \
  --config.file=/etc/alertmanager/alertmanager.yml --storage.path=/alertmanager >/dev/null
wait_for "alertmanager is ready" curl -fs http://127.0.0.1:19093/-/ready

docker run -d --name smoke-grafana --network "$network" -v "$secrets:/run/secrets:ro" \
  -p 127.0.0.1:13000:3000 \
  -e PROMETHEUS_URL=http://prometheus:9090 \
  -e GRAFANA_DASHBOARDS=/etc/grafana/dashboards \
  -e GF_SECURITY_ADMIN_PASSWORD__FILE=/run/secrets/grafana_password \
  release-watch/grafana >/dev/null
wait_for "grafana is ready" curl -fs http://127.0.0.1:13000/api/health
wait_for "the dashboard is provisioned" \
  curl -fs -u admin:smoke-password http://127.0.0.1:13000/api/dashboards/uid/release-watch
wait_for "grafana reaches prometheus" \
  curl -fs -u admin:smoke-password http://127.0.0.1:13000/api/datasources/uid/prometheus/health
