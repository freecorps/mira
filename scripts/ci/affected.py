"""Decide which CI jobs a change needs, and say why.

    python3 scripts/ci/affected.py --base <commit>   # compare <commit> with HEAD
    python3 scripts/ci/affected.py                   # no base: every job runs

Writes `python`, `ui` and `docker` (`true`/`false`) to $GITHUB_OUTPUT and a
table to $GITHUB_STEP_SUMMARY. Standard library only, so the job that runs it
installs nothing.

Each job's inputs are declared once, below, next to the reason a path is one.
The image's inputs are not written out at all: they are read from the
Dockerfile's own `COPY`/`ADD` lines, so a new `COPY` is covered the day it is
added. Anything unexpected (no base, a failed diff, a change to this script or
to ci.yml) runs every job: the failure mode is a slower run, never a skipped
check.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# A change here changes what the jobs are, so it runs all of them.
EVERY_JOB = (
    ".github/workflows/ci.yml",
    "scripts/ci/affected.py",
)

# A trailing slash is a directory prefix; anything else is one exact path.
PYTHON_INPUTS = (
    "src/",
    "tests/",
    "pyproject.toml",
    "uv.lock",
    # tests/test_orangepi_updater.py runs the real updater script.
    "deploy/orangepi/mira-update.sh",
    # tests/test_import_golden.py loads the script as a module.
    "scripts/import_golden_comments.py",
    # Picks which tests each shard of the job runs.
    "scripts/ci/pytest_shard.py",
)

UI_INPUTS = ("ui/",)

# What the image job exercises besides the build context: the smoke scripts
# it runs, the updater whose rollback it drives against real images, and the
# publish workflow whose build it stands in for.
DOCKER_EXTRA_INPUTS = (
    "Dockerfile",
    ".dockerignore",
    "scripts/ci/",
    "deploy/orangepi/",
    ".github/workflows/docker-publish.yml",
)


def dockerfile_sources(dockerfile: Path) -> list[str]:
    """The build-context paths the Dockerfile's COPY and ADD lines read.

    `COPY --from=<stage>` reads another stage, not the context, and is left
    out. A source of `.` means the whole context, returned as the empty
    prefix, which matches every path.
    """
    text = re.sub(r"\\\n", " ", dockerfile.read_text())
    sources: list[str] = []
    for line in text.splitlines():
        words = line.strip().split(maxsplit=1)
        if len(words) < 2 or words[0].upper() not in {"COPY", "ADD"}:
            continue
        rest = words[1]
        flags = re.findall(r"--\S+", rest)
        if any(flag.startswith("--from=") for flag in flags):
            continue
        rest = re.sub(r"--\S+\s*", "", rest).strip()
        args = json.loads(rest) if rest.startswith("[") else shlex.split(rest)
        for source in args[:-1]:
            source = source.removeprefix("./")
            if source in {"", "."}:
                sources.append("")
            elif match := re.search(r"[*?\[]", source):
                # A wildcard is covered by the directory it expands in.
                sources.append(source[: source.rfind("/", 0, match.start()) + 1])
            else:
                sources.append(source)
    return sources


def job_inputs() -> dict[str, tuple[str, ...]]:
    docker = (*dockerfile_sources(ROOT / "Dockerfile"), *DOCKER_EXTRA_INPUTS)
    return {"python": PYTHON_INPUTS, "ui": UI_INPUTS, "docker": docker}


def matches(path: str, inputs: tuple[str, ...]) -> bool:
    for entry in inputs:
        if entry.endswith("/") or entry == "":
            if path.startswith(entry):
                return True
        elif path == entry:
            return True
    return False


def changed_files(base: str) -> list[str] | None:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", base, "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"::warning::git diff against {base} failed: {result.stderr.strip()}")
        return None
    return [line for line in result.stdout.splitlines() if line]


def decide(files: list[str] | None) -> tuple[str, dict[str, list[str] | None]]:
    """Per job, the changed files that need it, or None when it runs anyway."""
    inputs = job_inputs()
    if files is None:
        return "no comparison available: every job runs", dict.fromkeys(inputs)
    whole = [path for path in files if path in EVERY_JOB]
    if whole:
        return f"{', '.join(whole)} changed: every job runs", dict.fromkeys(inputs)
    reason = f"{len(files)} changed file(s)"
    return reason, {job: [p for p in files if matches(p, globs)] for job, globs in inputs.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", help="commit to compare HEAD with")
    args = parser.parse_args()

    files = changed_files(args.base) if args.base else None
    reason, jobs = decide(files)

    lines = [
        f"### Affected jobs ({reason})",
        "",
        "| Job | Runs | Because of |",
        "| --- | --- | --- |",
    ]
    outputs = []
    for job, hits in jobs.items():
        runs = hits is None or bool(hits)
        if hits is None:
            why = "everything"
        elif hits:
            why = ", ".join(f"`{path}`" for path in hits[:5])
            if len(hits) > 5:
                why += f" and {len(hits) - 5} more"
        else:
            why = "nothing it reads changed"
        lines.append(f"| {job} | {'yes' if runs else 'no'} | {why} |")
        outputs.append(f"{job}={'true' if runs else 'false'}")

    report = "\n".join(lines)
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as fh:
            fh.write(report + "\n")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a") as fh:
            fh.write("\n".join(outputs) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
