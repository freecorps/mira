#!/usr/bin/env bash
set -Eeuo pipefail

candidate_image="${MIRA_CANDIDATE_IMAGE:-mira:ci-arm64}"
baseline_image="${MIRA_BASELINE_IMAGE:?MIRA_BASELINE_IMAGE must point to the deployed edge image}"
host_port="${MIRA_SMOKE_PORT:-18080}"
registry_port="${MIRA_SMOKE_REGISTRY_PORT:-15000}"
container_name="mira-arm64-smoke-${GITHUB_RUN_ID:-$$}"
registry_name="${container_name}-registry"
candidate_seed_name="${container_name}-candidate-seed"
# Tagged rather than anonymous so the exit trap can remove it whether the
# checks passed or `set -e` cut the run short at the first failure.
local_cli_image="mira:ci-arm64-local-cli-${GITHUB_RUN_ID:-$$}"
data_dir="$(mktemp -d)"
update_stack_dir="${data_dir}/updater-stack"
registry_image="127.0.0.1:${registry_port}/mira:edge"
failing_image="127.0.0.1:${registry_port}/mira:failing"
updater_log="${data_dir}/updater.log"
updater_script="$(pwd)/deploy/orangepi/mira-update.sh"

cleanup() {
  # Everything the containers wrote to the shared volume is owned by root
  # inside them, and the runner is not. A file at the top of `$data_dir` is
  # still removable — the runner owns the directory holding it — but a file
  # inside a directory the container created is not, and `verify_checks` puts
  # one under `indexes/<owner>/`. Hand the tree back before removing it, using
  # the image already on this machine so no extra pull is needed. Best effort:
  # a cleanup that failed must not fail a job whose assertions passed.
  if [[ -n "${candidate_image:-}" ]]; then
    docker run --rm --platform linux/arm64 \
      --volume "${data_dir}:/data" \
      --entrypoint chown "$candidate_image" \
      -R "$(id -u):$(id -g)" /data >/dev/null 2>&1 || true
  fi
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
  docker rmi "$local_cli_image" >/dev/null 2>&1 || true
  # `|| true` for the same reason as the chown above: this runs from an EXIT
  # trap, so a failure here would overwrite the exit status of the assertions
  # and fail a job that passed.
  rm -rf "$data_dir" || true
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

# Phase 6 pre-merge checks keep their two tables in the per-repository index
# database, which the deployed image created without them. Writing and reading
# a run through the candidate proves the upgrade is `CREATE TABLE IF NOT
# EXISTS` on connection rather than a migration step somebody has to sequence —
# and the rollback below then runs the deployed image against the same volume,
# which is the other half of the claim.
verify_checks() {
  local image="$1"
  docker run --rm     --platform linux/arm64     --volume "${data_dir}:/data"     --env MIRA_INDEX_DIR=/data/indexes     --entrypoint python     "$image"     -c 'from mira.checks.models import CheckResult, CheckRun, CheckRunInputs; from mira.index.store import IndexStore; store = IndexStore.open("phase-zero", "canary"); inputs = CheckRunInputs(owner="phase-zero", repo="canary", pr_number=1, head_sha="smoke"); run = CheckRun(run_key="smoke-run", policy_version="checks-v1+smoke", inputs=inputs, results=[CheckResult(check_id="native.tests", mode="warning", state="pass", result_key="smoke-result")]); store.record_check_run(run); store.record_check_run(run); assert store.count_check_runs({}) == 1, "a retried run must converge on one row"; assert store.latest_check_run(pr_number=1, head_sha="smoke").verdict == "pass"; store.close()'
}

# Phase 7C's triage tables, for the same reason as the check tables above:
# the deployed image created this volume's database without them, so writing
# and reading a run through the candidate proves the upgrade is `CREATE TABLE
# IF NOT EXISTS` on connection rather than a migration step somebody has to
# sequence. The idempotency of a path contribution is asserted here too — it
# is a UNIQUE constraint doing the work, and a constraint that behaved
# differently on this architecture would put whoever pushes most at the top of
# every ranking.
verify_triage() {
  local image="$1"
  docker run --rm     --platform linux/arm64     --volume "${data_dir}:/data"     --env MIRA_INDEX_DIR=/data/indexes     --entrypoint python     "$image"     -c 'from mira.index.store import IndexStore; from mira.triage.models import Evidence, ReviewerCandidate, SignalContribution, SignalReport, TriageInputs, TriageRun; store = IndexStore.open("phase-zero", "canary"); inputs = TriageInputs(owner="phase-zero", repo="canary", pr_number=1, head_sha="smoke"); run = TriageRun(run_key="smoke-triage", policy_version="triage-v1+smoke", inputs=inputs, candidates=[ReviewerCandidate(identity="dana", score=3.0, contributions=[SignalContribution(kind="codeowners", raw=1, weight=3.0, score=3.0, evidence=[Evidence(path="a.py", line=1, source="codeowners")])])], signals=[SignalReport(kind="codeowners", status="available", candidates=1)]); store.record_triage_run(run); store.record_triage_run(run); assert store.count_triage_runs({}) == 1, "a retried triage must converge on one row"; stored = store.latest_triage_run(pr_number=1, head_sha="smoke"); assert stored.suggested == ["dana"], stored.suggested; assert stored.status == "ok", stored.status; row = {"platform": "github", "path": "a.py", "identity": "dana", "role": "authored", "source": "commit", "reference": "abc", "event_at": 1.0}; assert store.record_path_contributions([row]) == 1; assert store.record_path_contributions([row]) == 0, "the same contribution must not be counted twice"; store.close()'
}

# Phase 7A's local review surface. Two halves, because they need different
# things from the environment.
#
# The first half needs no git: the exit-code table and the failure a caller
# gets when there is no work tree are a published contract, and a contract that
# holds on amd64 and prints a traceback on aarch64 is not one. The bounded
# subprocess the surface runs git through is checked here too, because rlimits
# are the part of `mira.sandbox` most likely to behave differently per
# architecture, and one that silently stopped applying would remove the only
# ceiling on a linter the check framework starts.
verify_local_cli_without_git() {
  local image=$1
  local table
  # Captured rather than piped into grep: `grep -q` closes the pipe on its
  # first match, and under `pipefail` a docker run killed by SIGPIPE would
  # fail a check that had just passed.
  table="$(docker run --rm --platform linux/arm64 --entrypoint mira \
    "$image" local review --explain-exit-codes)"
  case "$table" in
    *findings*) ;;
    *)
      echo "local review --explain-exit-codes printed no table: ${table}" >&2
      exit 1
      ;;
  esac

  local output status
  set +e
  output="$(docker run --rm --platform linux/arm64 --entrypoint mira \
    "$image" local review --path /app 2>&1)"
  status=$?
  set -e
  if [[ "$status" -ne 3 ]]; then
    echo "local review exited ${status} with no repository; expected 3" >&2
    echo "$output" >&2
    exit 1
  fi
  case "$output" in
    *Traceback*)
      echo "local review leaked a traceback instead of an exit code: ${output}" >&2
      exit 1
      ;;
  esac

  docker run --rm --platform linux/arm64 --entrypoint python \
    "$image" \
    -c 'from mira.sandbox import run_argv; o = run_argv(["python", "-c", "print(1)"], cwd="/app", timeout_seconds=30); assert o.status == "ok", o.detail; assert not o.unbounded, "rlimits did not apply on aarch64"; assert o.stdout.strip() == "1", o.stdout'
}

# The second half needs git, which the runtime image does not carry: the local
# CLI is a developer-machine tool and the server has no checkout to review, so
# putting git in the published image would grow every deployment for a command
# no deployment runs. A throwaway layer on top of the candidate gives the real
# path -- git subprocess, diff parsing, configuration, destination guard -- on
# aarch64 without changing what ships.
verify_local_cli_with_git() {
  local image=$1
  local helper="$local_cli_image"
  local context
  context="$(mktemp -d)"
  cat >"${context}/Dockerfile" <<DOCKERFILE
FROM ${image}
RUN apt-get update \\
 && apt-get install -y --no-install-recommends git \\
 && rm -rf /var/lib/apt/lists/*
DOCKERFILE
  docker build --platform linux/arm64 --tag "$helper" "$context" >/dev/null
  rm -rf "$context"
  docker run --rm --platform linux/arm64 \
    --volume "$(pwd)/scripts/ci:/ci:ro" \
    --entrypoint sh "$helper" /ci/arm64_local_cli_check.sh
}

# Phase 7B's read-only MCP server. No git and no model: the whole surface is
# SQLite reads, an audit write and a stdio pipe, all of which are the parts most
# likely to differ on the reference Orange Pi deployment. Driven through the
# real `mira mcp serve` process rather than the Python objects, because the
# pipe and the process's stdout handling are half of what is being checked.
verify_mcp() {
  local image=$1
  docker run --rm --platform linux/arm64 \
    --volume "$(pwd)/scripts/ci:/ci:ro" \
    --entrypoint sh "$image" /ci/arm64_mcp_check.sh
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
echo "Confirming the candidate creates and uses its check tables on ARM64"
verify_checks "$candidate_image"
echo "Confirming the candidate creates and uses its triage tables on ARM64"
verify_triage "$candidate_image"
echo "Confirming the local review surface on ARM64"
verify_local_cli_without_git "$candidate_image"
verify_local_cli_with_git "$candidate_image"
echo "Confirming the read-only MCP server on ARM64"
verify_mcp "$candidate_image"

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
