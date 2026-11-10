#!/usr/bin/env bash
# Downloads the exact Prometheus, Alertmanager and Grafana the tests run against,
# checks each archive against its published SHA-256, and unpacks into .tools/.
set -euo pipefail

PROMETHEUS_VERSION=3.16.0
ALERTMANAGER_VERSION=0.35.0
GRAFANA_VERSION=13.3.0

root="$(cd "$(dirname "$0")/.." && pwd)"
tools="${TOOLS_DIR:-$root/.tools}"
mkdir -p "$tools"

case "$(uname -s)" in
  Darwin) os=darwin ;;
  Linux) os=linux ;;
  *) echo "unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac
case "$(uname -m)" in
  arm64 | aarch64) arch=arm64 ;;
  x86_64) arch=amd64 ;;
  *) echo "unsupported CPU: $(uname -m)" >&2; exit 1 ;;
esac

sha256() {
  if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

fetch() { # name, archive url, expected sha, unpacked folder name
  local name="$1" url="$2" expected="$3"
  local archive="$tools/$name.tar.gz"
  curl -fsSL "$url" -o "$archive"
  local actual
  actual="$(sha256 "$archive")"
  if [ "$actual" != "$expected" ]; then
    echo "$name: checksum mismatch, expected $expected, got $actual" >&2
    rm -f "$archive"
    exit 1
  fi
  rm -rf "${tools:?}/$name"
  mkdir "$tools/$name"
  tar -xzf "$archive" -C "$tools/$name" --strip-components=1
  rm -f "$archive"
  echo "$name ready"
}

github_sha() { # owner/repo, version, archive file
  curl -fsSL "https://github.com/$1/releases/download/v$2/sha256sums.txt" | awk -v f="$3" '$2 == f { print $1 }'
}

if [ ! -x "$tools/prometheus/prometheus" ]; then
  f="prometheus-$PROMETHEUS_VERSION.$os-$arch.tar.gz"
  fetch prometheus "https://github.com/prometheus/prometheus/releases/download/v$PROMETHEUS_VERSION/$f" \
    "$(github_sha prometheus/prometheus "$PROMETHEUS_VERSION" "$f")"
fi

if [ ! -x "$tools/alertmanager/alertmanager" ]; then
  f="alertmanager-$ALERTMANAGER_VERSION.$os-$arch.tar.gz"
  fetch alertmanager "https://github.com/prometheus/alertmanager/releases/download/v$ALERTMANAGER_VERSION/$f" \
    "$(github_sha prometheus/alertmanager "$ALERTMANAGER_VERSION" "$f")"
fi

if [ ! -x "$tools/grafana/bin/grafana" ]; then
  url="https://dl.grafana.com/oss/release/grafana-$GRAFANA_VERSION.$os-$arch.tar.gz"
  fetch grafana "$url" "$(curl -fsSL "$url.sha256")"
fi
