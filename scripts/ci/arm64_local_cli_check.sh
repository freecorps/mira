#!/bin/sh
# Phase 7A on ARM64: the local review surface, exercised inside the aarch64
# image against a real git repository.
#
# Run *inside* the container by scripts/ci/smoke_arm64.sh, not on the runner.
# Everything here is architecture-sensitive in a way a unit test on the CI
# runner's amd64 Python cannot cover: git is a subprocess launched through
# mira.sandbox, which applies rlimits and process-group kills, and both of those
# behave differently per platform. A local review that silently degrades on the
# reference Orange Pi deployment is exactly what this file exists to catch.
#
# The model is never called. Everything up to the review — resolving the
# repository, producing the diff, resolving configuration, and refusing a
# redirected destination — runs here; the review itself is covered by the
# Python suite, which does not depend on the architecture.

set -eu

export HOME=/tmp/mira-arm64-home
export MIRA_INDEX_DIR=/tmp/mira-arm64-index
rm -rf "$HOME" "$MIRA_INDEX_DIR" /tmp/repo
mkdir -p "$HOME" "$MIRA_INDEX_DIR" /tmp/repo/src

git config --global user.email dev@example.com
git config --global user.name Dev
git config --global init.defaultBranch main
git config --global --add safe.directory /tmp/repo

cd /tmp/repo
git init -q .
git remote add origin https://github.com/acme/widgets.git
cat >.mira.yaml <<'YAML'
llm:
  model: anthropic/claude-sonnet-4-6
review:
  walkthrough: false
  code_context: false
YAML
printf 'def start():\n    return 1\n' >src/app.py
git add -A
git commit -qm "initial commit"
printf 'def start():\n    return 2\n' >src/app.py

echo "Resolving a working-tree review on aarch64"
python - <<'PY'
from mira.local import run as local_run
from mira.local.exit_codes import ExitCode
from mira.local.run import LocalReviewError

review = local_run.prepare(path="/tmp/repo")
assert review.identity.slug == "acme/widgets", review.identity
assert review.identity.platform == "github", review.identity
assert review.diff.mode == "working_tree", review.diff.mode
assert [entry.path for entry in review.diff.entries] == ["src/app.py"], review.diff.entries
assert "return 2" in review.diff.diff_text, review.diff.diff_text
assert review.destinations, "no destination was resolved"
assert all(d.vendor == "anthropic" for d in review.destinations), review.destinations

# The guard is the one thing here that must hold identically everywhere, so it
# is asserted on the deployment architecture rather than only on the runner's.
try:
    local_run.prepare(path="/tmp/repo", overrides={"llm.model": "openai/gpt-5"})
except LocalReviewError as exc:
    assert exc.code == ExitCode.CONFIG, exc.code
else:
    raise SystemExit("a redirected destination was not refused on aarch64")
PY

echo "Confirming the command's own exit codes on aarch64"
set +e
mira local review --path /tmp/repo --model openai/gpt-5 >/tmp/local-out 2>/tmp/local-err
redirect_status=$?
set -e
if [ "$redirect_status" -ne 4 ]; then
  echo "A redirected destination exited ${redirect_status}; expected 4" >&2
  cat /tmp/local-err >&2
  exit 1
fi
grep -q "Refusing to send" /tmp/local-err

set +e
mira local review --path /tmp/repo --range 'nosuchref..HEAD' >/tmp/local-out 2>/tmp/local-err
range_status=$?
set -e
if [ "$range_status" -ne 3 ]; then
  echo "An unknown revision exited ${range_status}; expected 3" >&2
  cat /tmp/local-err >&2
  exit 1
fi

set +e
mira local review --path /tmp/repo --staged --range 'HEAD~1..HEAD' >/tmp/local-out 2>/tmp/local-err
usage_status=$?
set -e
if [ "$usage_status" -ne 2 ]; then
  echo "Conflicting modes exited ${usage_status}; expected 2" >&2
  cat /tmp/local-err >&2
  exit 1
fi

echo "Local review surface passed on ARM64"
