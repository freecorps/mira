#!/usr/bin/env bash
set -Eeuo pipefail

candidate_image="${MIRA_CANDIDATE_IMAGE:-mira:ci-arm64}"
baseline_image="${MIRA_BASELINE_IMAGE:?MIRA_BASELINE_IMAGE must point to the deployed edge image}"
host_port="${MIRA_SMOKE_PORT:-18080}"
registry_port="${MIRA_SMOKE_REGISTRY_PORT:-15000}"
container_name="mira-arm64-smoke-${GITHUB_RUN_ID:-$$}"
registry_name="${container_name}-registry"
candidate_seed_name="${container_name}-candidate-seed"
data_dir="$(mktemp -d)"
update_stack_dir="${data_dir}/updater-stack"
registry_image="127.0.0.1:${registry_port}/mira:edge"
failing_image="127.0.0.1:${registry_port}/mira:failing"
updater_log="${data_dir}/updater.log"
updater_script="$(pwd)/deploy/orangepi/mira-update.sh"

cleanup() {
  if [[ -f "${update_stack_dir}/compose.yaml" ]]; then
    MIRA_IMAGE="$registry_image" \
      MIRA_SMOKE_CONTAINER_NAME="$container_name" \
      MIRA_SMOKE_DATA_DIR="$data_dir" \
      MIRA_SMOKE_PORT="$host_port" \
      docker compose -f "${update_stack_dir}/compose.yaml" down \
      >/dev/null 2>&1 || true
  fi
  docker rm -f "$container_name" "$candidate_seed_name" "$registry_name" \
    >/dev/null 2>&1 || true
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

echo "Exercising the real Orange Pi updater failure-and-restore path"
mkdir -p "$update_stack_dir"
cat >"${update_stack_dir}/compose.yaml" <<'YAML'
services:
  mira:
    image: ${MIRA_IMAGE}
    platform: linux/arm64
    container_name: ${MIRA_SMOKE_CONTAINER_NAME}
    ports:
      - "127.0.0.1:${MIRA_SMOKE_PORT}:8000"
    environment:
      ADMIN_PASSWORD: phase-zero-smoke-password
      DATABASE_URL: sqlite:////data/app.db
      MIRA_INDEX_DIR: /data/indexes
      MIRA_FORGEJO_TOKEN: smoke-token
      MIRA_FORGEJO_WEBHOOK_SECRET: smoke-secret
      MIRA_BOT_NAME: smoke-bot
    volumes:
      - "${MIRA_SMOKE_DATA_DIR}:/data"
YAML

docker run --detach --rm \
  --name "$registry_name" \
  --publish "127.0.0.1:${registry_port}:5000" \
  registry:2 >/dev/null
for ((attempt = 1; attempt <= 10; attempt++)); do
  if curl --fail --silent --max-time 2 \
    "http://127.0.0.1:${registry_port}/v2/" >/dev/null; then
    break
  fi
  if ((attempt == 10)); then
    echo "Local registry did not become ready" >&2
    exit 1
  fi
  sleep 1
done

docker tag "$baseline_image" "$registry_image"
docker push "$registry_image" >/dev/null
baseline_id="$(docker image inspect --format '{{.Id}}' "$registry_image")"

MIRA_IMAGE="$registry_image" \
  MIRA_SMOKE_CONTAINER_NAME="$container_name" \
  MIRA_SMOKE_DATA_DIR="$data_dir" \
  MIRA_SMOKE_PORT="$host_port" \
  docker compose -f "${update_stack_dir}/compose.yaml" up -d >/dev/null
wait_for_health "updater baseline"

docker create --platform linux/arm64 \
  --name "$candidate_seed_name" "$candidate_image" >/dev/null
docker commit \
  --change 'ENTRYPOINT ["python", "-c", "raise SystemExit(1)"]' \
  "$candidate_seed_name" "$failing_image" >/dev/null
docker rm "$candidate_seed_name" >/dev/null
docker tag "$failing_image" "$registry_image"
docker push "$registry_image" >/dev/null
# Keep the running baseline as the updater's local previous image while the
# registry serves the deliberately broken candidate on the same deployment tag.
docker tag "$baseline_id" "$registry_image"

set +e
MIRA_STACK_DIR="$update_stack_dir" \
  MIRA_SERVICE=mira \
  MIRA_IMAGE="$registry_image" \
  MIRA_HEALTH_URL="http://127.0.0.1:${host_port}/health" \
  MIRA_UPDATE_LOCK_FILE="${data_dir}/updater.lock" \
  MIRA_HEALTH_ATTEMPTS=60 \
  MIRA_HEALTH_INTERVAL_SECONDS=1 \
  MIRA_SMOKE_CONTAINER_NAME="$container_name" \
  MIRA_SMOKE_DATA_DIR="$data_dir" \
  MIRA_SMOKE_PORT="$host_port" \
  bash "$updater_script" 2>&1 | tee "$updater_log"
updater_status="${PIPESTATUS[0]}"
set -e

if [[ "$updater_status" -ne 1 ]]; then
  echo "Updater returned ${updater_status}; expected 1 after a healthy rollback" >&2
  exit 1
fi
grep -q "Rolling back to" "$updater_log"
grep -q "Rollback is healthy" "$updater_log"
wait_for_health "updater rollback"
running_image="$(docker inspect --format '{{.Image}}' "$container_name")"
if [[ "$running_image" != "$baseline_id" ]]; then
  echo "Updater left ${running_image} running instead of ${baseline_id}" >&2
  exit 1
fi
verify_canary "$baseline_image"

test -s "${data_dir}/app.db.pre-upgrade"
echo "ARM64 runtime, SQLite compatibility, and real updater rollback passed"
