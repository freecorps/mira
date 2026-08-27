"""Does the pull request say what it does?

The cheapest check in the framework and the one most likely to be argued with,
so it is written to be defensible rather than opinionated. It objects to four
things, each of which is a fact rather than a matter of taste:

* a title that carries no information — ``update``, ``fixes``, ``wip``, a bare
  ticket reference, or fewer characters than a filename;
* a description that is empty;
* a description that is still the repository's template, with the prompts left
  unanswered;
* a description made only of a checklist, with nothing said in prose.

It deliberately does *not* judge grammar, length beyond a floor, tone, or
whether the explanation is any good. Those are review comments, and Mira
already writes review comments; a pre-merge check that blocks a merge on a
matter of taste is a check teams turn off.

Drafts get one concession: a draft titled ``WIP`` is telling the truth, and
objecting to it would be objecting to the draft flag. Every other rule still
applies, because a draft with an empty description is still a pull request
nobody can review.
"""

from __future__ import annotations

import re

from mira.checks.context import CheckContext, CheckOutcome
from mira.checks.models import CheckFinding, Evidence, fingerprint
from mira.checks.native.evidence import find_in_body, snippet

VERSION = "1"

# Characters below which a title cannot be describing anything. Deliberately
# low: the check is looking for empty, not for terse.
MIN_TITLE_LENGTH = 12
MIN_BODY_LENGTH = 30

# Titles that are grammatical but say nothing. Matched whole, after stripping a
# conventional-commit prefix, so `fix(gate): stop approving on unknown CI`
# passes and a bare `fix` does not.
_EMPTY_TITLES = re.compile(
    r"^(?:wip|tmp|temp|test|tests|testing|update|updates|updated|fix|fixes|fixed|"
    r"changes|change|misc|stuff|cleanup|refactor|patch|minor|small|quick|"
    r"bug ?fix|hotfix|new|init|initial commit|untitled|no ?title)$",
    re.IGNORECASE,
)

# A conventional-commit prefix carries real information (`feat`, `fix(scope)`),
# so it is stripped before judging what follows rather than counted as content.
_CONVENTIONAL_PREFIX = re.compile(r"^[a-z]+(?:\([^)]*\))?!?:\s*", re.IGNORECASE)

# A title that is nothing but a ticket reference.
_BARE_REFERENCE = re.compile(r"^(?:#\d+|[A-Z][A-Z0-9]+-\d+|gh-\d+)$", re.IGNORECASE)

# Titles a *draft* is allowed to carry. A draft announcing itself as unfinished
# is accurate, and a check that objected would be objecting to the draft flag.
_DRAFT_TITLES = frozenset({"wip", "tmp", "temp", "draft", "wip:"})

# Prompts a repository template leaves behind when nobody fills it in.
_TEMPLATE_PROMPTS = re.compile(
    r"(?i)(?:"
    r"<!--\s*(?:describe|explain|why|what|remove this|delete this)[^>]*-->"
    r"|\b(?:describe your changes|description of the change|"
    r"please describe|explain the motivation|replace this text|"
    r"add a description here|write a summary|your text here|"
    r"tbd|to ?be ?filled|fill (?:this )?in|xxx+)\b"
    r")"
)

_CHECKLIST_LINE = re.compile(r"^\s*(?:[-*]\s*\[[ xX]\]|\d+\.\s*\[[ xX]\])")
_COMMENT_LINE = re.compile(r"^\s*(?:<!--.*?-->\s*)+$", re.DOTALL)


def _prose(body: str) -> str:
    """The body with checklists, HTML comments and blank lines removed.

    What is left is what somebody actually wrote. A pull request whose entire
    description is a ticked template checklist has told a reviewer that a
    process was followed and nothing at all about the change.
    """
    kept: list[str] = []
    for line in (body or "").splitlines():
        stripped = line.strip()
        if not stripped or _CHECKLIST_LINE.match(line) or _COMMENT_LINE.match(line):
            continue
        if stripped.startswith("#"):
            # A heading is structure, not content.
            continue
        kept.append(stripped)
    return " ".join(kept).strip()


def _title_core(title: str) -> str:
    return _CONVENTIONAL_PREFIX.sub("", (title or "").strip()).strip()


def _finding(check: str, title: str, detail: str, evidence: list[Evidence]) -> CheckFinding:
    first = evidence[0] if evidence else Evidence()
    return CheckFinding(
        fingerprint=fingerprint(path=first.path, signature=check),
        title=title,
        detail=detail,
        evidence=evidence,
        sources=["native.title_description"],
    )


async def run(ctx: CheckContext) -> CheckOutcome:
    """Judge the title and the description, quoting whichever offends."""
    title = (ctx.pr_title or "").strip()
    body = ctx.pr_body or ""
    findings: list[CheckFinding] = []

    title_evidence = Evidence(
        path="",
        detail="pull request title",
        snippet=snippet(title) or "(empty)",
        source="pr",
        url=ctx.pr_url,
    )

    core = _title_core(title)
    # A draft that says WIP is telling the truth, and objecting to it would be
    # objecting to the draft flag. The concession covers the whole title
    # judgement rather than one branch of it: a title exempted from "says
    # nothing" and then failed for being three characters long would be the
    # same objection wearing a different message.
    draft_placeholder = ctx.draft and core.lower() in _DRAFT_TITLES

    if not title:
        findings.append(
            _finding(
                "title_empty",
                "The pull request has no title",
                "A title is the only part of a pull request most people read.",
                [title_evidence],
            )
        )
    elif draft_placeholder:
        pass
    elif _BARE_REFERENCE.match(core):
        findings.append(
            _finding(
                "title_bare_reference",
                "The title is only a ticket reference",
                f"{title!r} tells a reader which ticket this is, not what the change does.",
                [title_evidence],
            )
        )
    elif _EMPTY_TITLES.match(core):
        findings.append(
            _finding(
                "title_uninformative",
                "The title says nothing about the change",
                f"{title!r} would fit almost any pull request in this repository.",
                [title_evidence],
            )
        )
    elif len(core) < MIN_TITLE_LENGTH:
        findings.append(
            _finding(
                "title_too_short",
                "The title is too short to describe the change",
                f"{title!r} is {len(core)} characters after its prefix; "
                f"{MIN_TITLE_LENGTH} is the floor this repository checks for.",
                [title_evidence],
            )
        )

    prose = _prose(body)
    if not body.strip():
        findings.append(
            _finding(
                "body_empty",
                "The pull request has no description",
                "Nothing here explains why the change is being made, so a reviewer "
                "has only the diff to go on.",
                [
                    Evidence(
                        detail="pull request description",
                        snippet="(empty)",
                        source="pr",
                        url=ctx.pr_url,
                    )
                ],
            )
        )
    else:
        line_no, line = find_in_body(body, _TEMPLATE_PROMPTS)
        if line_no:
            findings.append(
                _finding(
                    "body_template_unfilled",
                    "The description is still the template",
                    "A template prompt was left in place of an answer.",
                    [
                        Evidence(
                            start_line=line_no,
                            detail="pull request description",
                            snippet=snippet(line),
                            source="pr",
                            url=ctx.pr_url,
                        )
                    ],
                )
            )
        elif not prose:
            findings.append(
                _finding(
                    "body_no_prose",
                    "The description is a checklist and nothing else",
                    "Every line is a checkbox, a heading or an HTML comment, so the "
                    "description records that a process was followed and not what changed.",
                    [
                        Evidence(
                            start_line=1,
                            detail="pull request description",
                            snippet=snippet(body.splitlines()[0] if body.splitlines() else ""),
                            source="pr",
                            url=ctx.pr_url,
                        )
                    ],
                )
            )
        elif len(prose) < MIN_BODY_LENGTH:
            findings.append(
                _finding(
                    "body_too_short",
                    "The description is too short to explain the change",
                    f"{len(prose)} characters of prose; {MIN_BODY_LENGTH} is the floor "
                    "this repository checks for.",
                    [
                        Evidence(
                            start_line=1,
                            detail="pull request description",
                            snippet=snippet(prose),
                            source="pr",
                            url=ctx.pr_url,
                        )
                    ],
                )
            )

    if findings:
        return CheckOutcome.violation(
            summary=(f"{len(findings)} problem(s) with how this pull request describes itself."),
            findings=findings,
        )
    return CheckOutcome.passed(
        summary="The title and description both say what this change does.",
        evidence=[title_evidence],
    )
