#!/usr/bin/env bash
set -Eeuo pipefail

candidate_image="${MIRA_CANDIDATE_IMAGE:-mira:ci-arm64}"
baseline_image="${MIRA_BASELINE_IMAGE:?MIRA_BASELINE_IMAGE must point to the deployed edge image}"
host_port="${MIRA_SMOKE_PORT:-18080}"
container_name="mira-arm64-smoke-${GITHUB_RUN_ID:-$$}"
data_dir="$(mktemp -d)"

cleanup() {
  docker rm -f "$container_name" >/dev/null 2>&1 || true
  rm -rf "$data_dir"
}
trap cleanup EXIT

wait_for_health() {
  local label="$1"
  for _ in $(seq 1 36); do
    if curl --fail --silent --show-error --max-time 3 \
      "http://127.0.0.1:${host_port}/health" >/dev/null 2>&1; then
      return 0
    fi
    if ! docker inspect "$container_name" >/dev/null 2>&1; then
      break
    fi
    sleep 5
  done
  echo "${label} image did not become healthy" >&2
  docker logs "$container_name" >&2 || true
  return 1
}

start_server() {
  local image="$1"
  local label="$2"
  docker rm -f "$container_name" >/dev/null 2>&1 || true
  docker run --detach --rm \
    --name "$container_name" \
    --platform linux/arm64 \
    --publish "127.0.0.1:${host_port}:8000" \
    --volume "${data_dir}:/data" \
    --env ADMIN_PASSWORD=phase-zero-smoke-password \
    --env DATABASE_URL=sqlite:////data/app.db \
    --env MIRA_INDEX_DIR=/data/indexes \
    --env MIRA_FORGEJO_TOKEN=smoke-token \
    --env MIRA_FORGEJO_WEBHOOK_SECRET=smoke-secret \
    --env MIRA_BOT_NAME=smoke-bot \
    "$image" >/dev/null
  wait_for_health "$label"
  docker stop --time 15 "$container_name" >/dev/null
}

verify_canary() {
  local image="$1"
  docker run --rm \
    --platform linux/arm64 \
    --volume "${data_dir}:/data" \
    --entrypoint python \
    "$image" \
    -c 'from mira.dashboard.db import AppDatabase; db = AppDatabase(url="sqlite:////data/app.db", admin_password="phase-zero-smoke-password"); repo = db.get_repo("phase-zero", "canary"); assert repo is not None and repo.installation_id == 4242; db.close()'
}

echo "Pulling deployed ARM64 baseline: ${baseline_image}"
docker pull --platform linux/arm64 "$baseline_image"

echo "Confirming candidate executes as ARM64"
docker run --rm --platform linux/arm64 --entrypoint python "$candidate_image" \
  -c 'import platform; assert platform.machine() in {"aarch64", "arm64"}, platform.machine()'

echo "Creating a real SQLite database with the deployed image"
docker run --rm \
  --platform linux/arm64 \
  --volume "${data_dir}:/data" \
  --entrypoint python \
  "$baseline_image" \
  -c 'from mira.dashboard.db import AppDatabase; db = AppDatabase(url="sqlite:////data/app.db", admin_password="phase-zero-smoke-password"); db.register_repo("phase-zero", "canary", installation_id=4242); db.close()'
cp "${data_dir}/app.db" "${data_dir}/app.db.pre-upgrade"

echo "Starting the candidate against the existing SQLite database"
start_server "$candidate_image" candidate
verify_canary "$candidate_image"

echo "Starting the deployed image against the candidate-opened database"
start_server "$baseline_image" rollback
verify_canary "$baseline_image"

test -s "${data_dir}/app.db.pre-upgrade"
echo "ARM64 runtime, SQLite compatibility, health check, and app rollback passed"
