"""Removing secrets and personal data before anything reaches a model.

Autofix sends more repository content to a model than a review does: whole file
bodies, CI output, command output. Any of those can carry a credential that was
committed by accident, and a credential that reaches a third-party inference
endpoint has left the building whatever happens next.

Two rules shape what is here.

**Redaction is applied at the boundary, not at the source.** Every function
that builds model input runs its text through :func:`redact`, so adding a new
context source cannot forget to. The stored artifacts — the patch, the diff,
the validation output shown in the dashboard — are redacted the same way, since
a secret in an audit record is still a secret in a database.

**A false positive is cheap and a miss is not.** These patterns deliberately
over-match. Replacing a harmless hex blob with a placeholder costs the model a
little context; letting a live key through costs a rotation.

What this is not: a scanner. It makes no findings and reports nothing to
anybody. It is a filter with one job, and it fails towards redacting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The placeholder is deliberately conspicuous and deliberately stable: a model
# that sees it should treat it as an opaque token, and a human reading a stored
# diff should be able to tell redaction from a variable actually named that.
PLACEHOLDER = "[REDACTED:{label}]"


@dataclass(frozen=True)
class _Rule:
    label: str
    pattern: re.Pattern[str]
    # Which capture group holds the secret. 0 replaces the whole match, which
    # is right for a self-contained token and wrong for `key = "..."`, where
    # the assignment has to survive for the code to still read as code.
    group: int = 0


def _rule(label: str, source: str, *, group: int = 0, flags: int = 0) -> _Rule:
    return _Rule(label=label, pattern=re.compile(source, flags), group=group)


# Ordered: the specific vendor shapes run before the generic assignment sweep,
# so a GitHub token in `token = "ghp_…"` is labelled as one rather than as an
# anonymous secret.
_RULES: tuple[_Rule, ...] = (
    # Multi-line PEM blocks, private keys of every flavour.
    _rule(
        "private-key",
        r"-----BEGIN[ A-Z]*PRIVATE KEY-----.*?-----END[ A-Z]*PRIVATE KEY-----",
        flags=re.DOTALL,
    ),
    # GitHub: classic PAT, fine-grained PAT, App/installation, OAuth, refresh.
    _rule("github-token", r"gh[pousr]_[A-Za-z0-9]{16,}"),
    _rule("github-token", r"github_pat_[A-Za-z0-9_]{20,}"),
    # GitLab personal / project / runner tokens.
    _rule("gitlab-token", r"glpat-[A-Za-z0-9\-_]{16,}"),
    _rule("gitlab-token", r"gl(?:rt|ptt|soat|cbt|dt|ft)-[A-Za-z0-9\-_]{16,}"),
    # Slack bot/user/app tokens and webhooks.
    _rule("slack-token", r"xox[abprs]-[A-Za-z0-9\-]{10,}"),
    _rule("slack-webhook", r"https://hooks\.slack\.com/services/[A-Za-z0-9/+_-]{20,}"),
    # AWS access key ids, and the secret they travel with.
    _rule("aws-access-key", r"(?:A3T[A-Z0-9]|AKIA|ABIA|ACCA|ASIA)[A-Z0-9]{16}"),
    _rule(
        "aws-secret-key",
        r"(?i)aws[_\- ]?secret[_\- ]?access[_\- ]?key[\"']?\s*[:=]\s*"
        r"[\"']?(?!\[REDACTED:)([A-Za-z0-9/+=]{40})",
        group=1,
    ),
    # Google API keys and OAuth client secrets.
    _rule("google-api-key", r"AIza[0-9A-Za-z\-_]{35}"),
    _rule("google-oauth", r"[0-9]+-[0-9a-z_]{32}\.apps\.googleusercontent\.com"),
    # Stripe, SendGrid, Twilio, npm, PyPI, OpenAI, Anthropic.
    _rule("stripe-key", r"[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    _rule("sendgrid-key", r"SG\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}"),
    _rule("twilio-sid", r"AC[0-9a-fA-F]{32}"),
    _rule("npm-token", r"npm_[A-Za-z0-9]{30,}"),
    _rule("pypi-token", r"pypi-AgEIcHlwaS5vcmc[A-Za-z0-9\-_]{20,}"),
    # Anthropic first: an `sk-ant-…` key also satisfies the OpenAI shape, and
    # a token labelled as the wrong vendor is a rotation aimed at the wrong
    # dashboard.
    _rule("anthropic-key", r"sk-ant-[A-Za-z0-9\-_]{20,}"),
    _rule("openai-key", r"sk-(?!ant-)(?:proj-)?[A-Za-z0-9\-_]{20,}"),
    # JSON Web Tokens — three base64url segments with the JWT header prefix.
    _rule("jwt", r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    # Credentials embedded in a URL: scheme://user:secret@host.
    _rule("url-credentials", r"(?<=://)[^\s/:@]+:([^\s/@]{3,})(?=@)", group=1),
    # Generic assignment: `password = "…"`, `api-key: '…'`, `SECRET=…`. The
    # value is replaced and the assignment kept, so the surrounding code still
    # parses and the model can still reason about the line.
    _rule(
        "secret",
        r"(?i)\b(?:pass(?:wd|word)?|secret|token|api[_\-]?key|access[_\-]?key|"
        r"private[_\-]?key|client[_\-]?secret|auth)\b[\"']?\s*[:=]\s*"
        r"[\"'](?!\[REDACTED:)([^\"'\n]{8,})[\"']",
        group=1,
    ),
    # Email addresses. Personal data rather than a credential, but it is the
    # one identifier a repository is guaranteed to contain.
    _rule(
        "email",
        r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
    ),
)

# Emails that identify a machine rather than a person. Redacting these buys no
# privacy and loses real context: `noreply@github.com` in a commit trailer is
# how a fix knows a commit was made by a bot.
_EMAIL_KEEP = re.compile(
    r"(?i)^(?:noreply|no-reply|donotreply|do-not-reply|support|admin|root|example|"
    r"user|you|your\.name|test)@|@(?:example\.(?:com|org|net)|localhost|noreply\.)",
)


def _replace(rule: _Rule, match: re.Match[str]) -> str:
    token = PLACEHOLDER.format(label=rule.label)
    if rule.group == 0:
        if rule.label == "email" and _EMAIL_KEEP.search(match.group(0)):
            return match.group(0)
        return token
    whole = match.group(0)
    secret = match.group(rule.group)
    if not secret:
        return whole
    start = match.start(rule.group) - match.start(0)
    end = match.end(rule.group) - match.start(0)
    return whole[:start] + token + whole[end:]


def redact(text: str) -> str:
    """Return ``text`` with every recognised secret or identifier replaced.

    Idempotent: running it twice produces the same string, because the
    placeholder matches none of the patterns.
    """
    if not text:
        return ""
    out = text
    for rule in _RULES:
        out = rule.pattern.sub(lambda match, rule=rule: _replace(rule, match), out)  # type: ignore[misc]
    return out


def redact_all(values: dict[str, str]) -> dict[str, str]:
    """Redact every value in a mapping, keys untouched."""
    return {key: redact(value) for key, value in values.items()}


def contains_secret(text: str) -> bool:
    """Whether redaction would change this text. Used by tests and by the
    publish step, which refuses to commit content that still looks like a
    credential even after the model handed it back."""
    return redact(text) != (text or "")
