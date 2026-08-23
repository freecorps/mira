#!/usr/bin/env bash
set -Eeuo pipefail

stack_dir="${MIRA_STACK_DIR:-/mnt/sda1/mira-stack}"
service="${MIRA_SERVICE:-mira}"
image="${MIRA_IMAGE:-ghcr.io/freecorps/mira:edge}"
health_url="${MIRA_HEALTH_URL:-http://127.0.0.1:8000/health}"
lock_file="${MIRA_UPDATE_LOCK_FILE:-/run/lock/mira-update.lock}"
health_attempts="${MIRA_HEALTH_ATTEMPTS:-24}"
health_interval="${MIRA_HEALTH_INTERVAL_SECONDS:-5}"

exec 9>"$lock_file"
if ! flock -n 9; then
  echo "Another Mira update is already running"
  exit 0
fi

cd "$stack_dir"

previous_image="$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null || true)"
container_id="$(docker compose ps -q "$service")"
running_image=""
if [[ -n "$container_id" ]]; then
  running_image="$(docker inspect --format '{{.Image}}' "$container_id")"
fi

docker compose pull "$service"
candidate_image="$(docker image inspect --format '{{.Id}}' "$image")"

if [[ "$candidate_image" == "$previous_image" && "$running_image" == "$candidate_image" ]]; then
  echo "Mira is already current: $candidate_image"
  exit 0
fi

docker compose up -d --no-deps "$service"

for ((attempt = 1; attempt <= health_attempts; attempt++)); do
  if curl --fail --silent --show-error --max-time 3 "$health_url" >/dev/null; then
    echo "Mira is healthy on image $candidate_image"
    exit 0
  fi
  if ((attempt < health_attempts)); then
    sleep "$health_interval"
  fi
done

echo "Mira failed its health check after the update" >&2
docker compose logs --tail=80 "$service" >&2 || true

if [[ -z "$previous_image" ]]; then
  echo "No previous image is available for rollback" >&2
  exit 1
fi

echo "Rolling back to $previous_image" >&2
docker image tag "$previous_image" "$image"
docker compose up -d --no-deps --force-recreate "$service"

for ((attempt = 1; attempt <= health_attempts; attempt++)); do
  if curl --fail --silent --show-error --max-time 3 "$health_url" >/dev/null; then
    echo "Rollback is healthy"
    exit 1
  fi
  if ((attempt < health_attempts)); then
    sleep "$health_interval"
  fi
done

echo "Rollback also failed its health check" >&2
exit 1
