"""Where the code in the diff is allowed to go, and nowhere else.

A local review reads a developer's uncommitted work and sends it to a model.
That is the same thing the server does on a pull request, with one difference
that matters: on the server, the destination is a deployment decision made
once, in configuration that a reviewer approved. On a laptop it is a command
line, an environment variable and whatever ``.mira.yaml`` happened to be found
by walking up from the current directory — three ways to send a repository's
source to a vendor the repository never agreed to.

So this module answers one question before any code is read: *is the endpoint
this run is about to use the same one the repository is configured for?* If it
is not, the run stops. There is no override flag. A flag to disable this would
be the feature, and it would be used by exactly the person who should not have
it.

Three pieces make it work.

**Configuration is anchored to the repository, not to the process.**
:func:`repo_config_path` resolves ``.mira.yaml`` from the repository root that
is actually being reviewed. Without that, running ``mira local review`` on a
sibling checkout would apply *this* directory's configuration to *that*
directory's code — which is the whole failure mode in one command.

**The destination comes from the base, not from the change.** The working
tree's ``.mira.yaml`` is part of what is being reviewed. If it decided where the
code went, a branch could add four lines naming an attacker's ``base_url`` and
an ``api_key_env`` pointing at any environment variable on the machine, and the
guard would compare that file against itself and agree. So the baseline is read
with ``git show <base>:.mira.yaml`` — the committed file at the commit the
review is measured against — and a change that moves the destination is refused
rather than obeyed. Everything else the working tree's configuration says still
applies: thresholds, filters and check policy decide how the review reads, not
who receives it.

**The comparison is against that trusted answer.** The baseline is built the
way any other configuration is: deployment defaults, dashboard overrides, then
the committed repository file, with the process environment's ``MIRA_MODEL``
removed. The effective destination is the same computation over the working
tree's file with the command line's overrides applied. Anything the command
line or the branch moved is therefore visible, and anything the deployment
already decided cancels out.

Comparison is on the endpoint and the model *vendor*, not on the exact model
id. Switching from a vendor's small model to its large one is a cost decision
and stays allowed; switching the vendor, the endpoint, the credential or the
API protocol is a different recipient of the source code and does not.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mira.config import MiraConfig, load_config

#: Every purpose that is handed repository content. All three are checked: a
#: guard that only looked at the review tier would let ``indexing_model`` carry
#: the same source to a different vendor.
CONTENT_PURPOSES = ("review", "indexing", "security")

#: The per-repository configuration filenames, most preferred first. Kept in
#: step with `mira.config`, which discovers the same names by walking upwards.
CONFIG_FILENAMES = (".mira.yaml", ".mira.yml")


class DestinationRefused(Exception):
    """The run would have sent repository code somewhere it was not configured to.

    Carries the two destinations so the message can name both, because "refused"
    without "from here to there" is not actionable.
    """

    def __init__(self, purpose: str, configured: Destination, requested: Destination) -> None:
        self.purpose = purpose
        self.configured = configured
        self.requested = requested
        super().__init__(
            f"Refusing to send this repository's code to a different {purpose} destination.\n"
            f"  configured at the base of this review: {configured.describe()}\n"
            f"  this command would have used:          {requested.describe()}\n"
            "The destination comes from .mira.yaml as committed at the base, not from "
            "the working tree, because a change under review must not be able to choose "
            "where it is sent. Commit the new destination on the base first."
        )


@dataclass(frozen=True)
class Destination:
    """Everything that decides *who* receives the code, and nothing else.

    Deliberately excludes the model id: two models from one vendor over one
    endpoint are one recipient. It includes ``api_key_env`` because a different
    credential is a different account, and ``api_style`` because the protocol
    determines what is transmitted and where.
    """

    purpose: str
    provider: str
    endpoint: str
    vendor: str
    api_key_env: str
    api_style: str
    #: Reported, never compared.
    model: str = ""

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (self.provider, self.endpoint, self.vendor, self.api_key_env, self.api_style)

    def describe(self) -> str:
        if self.provider == "bedrock":
            return f"bedrock {self.endpoint} (model {self.model or 'unset'})"
        credential = self.api_key_env or "no credential"
        return f"{self.endpoint} via {credential} (model {self.model or 'unset'})"

    def as_dict(self) -> dict[str, str]:
        return {
            "purpose": self.purpose,
            "provider": self.provider,
            "endpoint": self.endpoint,
            "vendor": self.vendor,
            "api_key_env": self.api_key_env,
            "api_style": self.api_style,
            "model": self.model,
        }


def _vendor_of(model: str) -> str:
    """The part of a model id that names who runs it.

    ``anthropic/claude-sonnet-4-6`` → ``anthropic``. A bare id with no prefix
    has no vendor to compare, so it compares as empty — which makes two bare ids
    on the same endpoint equal, and that is the right answer: the endpoint is
    then the only recipient either of them names.
    """
    name = (model or "").strip()
    if "/" not in name:
        return ""
    return name.split("/", 1)[0].lower()


def _endpoint_of(llm: Any) -> str:
    if getattr(llm, "provider", "") == "bedrock":
        return f"aws:{getattr(llm, 'region', '') or 'unset'}"
    parts = urlsplit(getattr(llm, "base_url", "") or "")
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port else ""
    path = (parts.path or "").rstrip("/")
    return f"{parts.scheme}://{host}{port}{path}"


def destination_for(config: MiraConfig, purpose: str) -> Destination:
    """The destination one purpose's client would actually talk to.

    Resolved through the same helper the engine uses, so a dashboard override
    or a per-purpose model in ``.mira.yaml`` is reflected here exactly as it
    will be at call time.
    """
    from mira.dashboard.models_config import llm_config_for

    resolved = llm_config_for(purpose, config.llm)
    return Destination(
        purpose=purpose,
        provider=(resolved.provider or "").lower(),
        endpoint=_endpoint_of(resolved),
        vendor=_vendor_of(resolved.model),
        api_key_env=resolved.api_key_env or "",
        api_style=(resolved.api_style or "").lower(),
        model=resolved.model or "",
    )


def repo_config_path(repo_root: Path) -> Path | None:
    """The repository's own ``.mira.yaml``, or None when it has none.

    Only the root is consulted. Walking upwards from the root would leave a
    configuration file in a parent directory — a home directory, a workspace
    folder holding several checkouts — deciding where one repository's code is
    sent, which is precisely the ambient authority this module exists to remove.
    """
    for name in CONFIG_FILENAMES:
        candidate = repo_root / name
        if candidate.is_file():
            return candidate
    return None


@contextmanager
def _without_env(*names: str) -> Iterator[None]:
    """Run a block with some environment variables temporarily unset."""
    saved = {name: os.environ[name] for name in names if name in os.environ}
    for name in saved:
        del os.environ[name]
    try:
        yield
    finally:
        os.environ.update(saved)


def apply_deployment_defaults(config_path: str | Path) -> None:
    """Layer a deployment-wide config file underneath the repository's own.

    Same meaning as ``mira serve --config``: defaults, not overrides. Loading it
    *underneath* ``.mira.yaml`` rather than instead of it is what keeps the
    repository's own settings in force — a ``--config`` that replaced them would
    be a way to review a repository under somebody else's rules, including
    somebody else's model endpoint.
    """
    from mira.config import set_global_defaults

    set_global_defaults(config_path)


def load_repo_config(
    repo_root: Path,
    overrides: dict[str, Any] | None = None,
) -> MiraConfig:
    """Load configuration for a repository, anchored at its root.

    The repository's own ``.mira.yaml`` is found from the root rather than by
    walking up from the current directory, so reviewing a checkout from
    somewhere else applies that checkout's rules and not this one's.
    """
    return load_config(repo_config_path(repo_root), overrides)


def committed_config_text(repo_root: Path, base_rev: str) -> str:
    """``.mira.yaml`` as committed at ``base_rev``, or "" when there is none.

    ``git show`` rather than the file on disk, because the file on disk is part
    of what is being reviewed. Both accepted filenames are tried, in the same
    order :func:`repo_config_path` tries them.
    """
    from mira.local.gitcmd import GitError, run_git

    if not base_rev:
        return ""
    for name in CONFIG_FILENAMES:
        try:
            result = run_git(repo_root, "show", f"{base_rev}:{name}")
        except GitError:
            return ""
        if result.ok:
            return result.stdout
    return ""


def trusted_config(repo_root: Path, base_rev: str) -> MiraConfig:
    """The configuration the destination is allowed to come from.

    Deployment defaults, dashboard overrides, and ``.mira.yaml`` **as committed
    at the base of what is being reviewed** — never the working tree's copy, and
    never a file discovered by walking up from the current directory. The
    committed text is materialised into a temporary file so it goes through the
    same loader, validator and layering as any other configuration; an empty
    file is written when the base carries none, because passing ``None`` would
    send :func:`mira.config.load_config` back to walking up from the current
    directory and straight into the file this function exists to distrust.
    """
    text = committed_config_text(repo_root, base_rev)
    with tempfile.TemporaryDirectory(prefix="mira-local-baseline-") as scratch:
        path = Path(scratch) / CONFIG_FILENAMES[0]
        path.write_text(text, encoding="utf-8")
        with _without_env("MIRA_MODEL"):
            return load_config(path)


def check_destinations(
    repo_root: Path, *, effective: MiraConfig, base_rev: str
) -> list[Destination]:
    """Compare the run's destinations with the trusted ones, or raise.

    ``base_rev`` is the commit the review is measured against — ``HEAD`` for the
    working tree and the index, the range's base for a range. It is what makes
    the comparison meaningful: the working tree's ``.mira.yaml`` is *part of the
    change under review*, so letting it name the destination would mean a branch
    could redirect its own review to any endpoint, under any environment
    variable named as the credential. The trusted answer therefore comes from
    the committed file at the base, and a change that moves the destination is
    refused rather than obeyed.

    Everything else in the working tree's configuration still applies —
    thresholds, filters, check policy — because none of it decides who receives
    the code.

    Returns the effective destinations, in :data:`CONTENT_PURPOSES` order, so
    the caller can report where the code went.
    """
    baseline = trusted_config(repo_root, base_rev)

    destinations: list[Destination] = []
    for purpose in CONTENT_PURPOSES:
        configured = destination_for(baseline, purpose)
        requested = destination_for(effective, purpose)
        if configured.key != requested.key:
            raise DestinationRefused(purpose, configured, requested)
        destinations.append(requested)
    return destinations
