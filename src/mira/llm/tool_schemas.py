"""Structured-output tool schemas the model fills in via tool calling.

The ``submit_*`` schemas for the review, critique, thread-reply, and
walkthrough passes. Distinct from ``agentic_tools`` (the read_file/grep
tools the model calls while reasoning) — these are the shapes it returns.
"""

SUBMIT_REVIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_review",
        "description": "Submit your code review findings including comments, key issues, and a summary.",
        "parameters": {
            "type": "object",
            "properties": {
                "comments": {
                    "type": "array",
                    "description": "List of review comments on specific lines of code.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative file path."},
                            "line": {
                                "type": "integer",
                                "description": "Line number in the target file.",
                            },
                            "end_line": {
                                "type": ["integer", "null"],
                                "description": "End line for multi-line comments, or null.",
                            },
                            "severity": {
                                "type": "string",
                                "enum": ["blocker", "warning", "suggestion", "nitpick"],
                            },
                            "category": {
                                "type": "string",
                                "enum": [
                                    "bug",
                                    "security",
                                    "performance",
                                    "error-handling",
                                    "race-condition",
                                    "resource-leak",
                                    "maintainability",
                                    "clarity",
                                    "configuration",
                                    "other",
                                ],
                            },
                            "title": {"type": "string", "description": "Short title (<80 chars)."},
                            "body": {
                                "type": "string",
                                "description": "Detailed explanation of the issue. Use single backticks for inline code references. Do NOT use triple-backtick code blocks.",
                            },
                            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                            "existing_code": {
                                "type": "string",
                                "description": "Verbatim copy of the code from the diff that this comment targets. Must be an exact substring.",
                            },
                            "suggestion": {
                                "type": ["string", "null"],
                                "description": "Optional replacement code to fix the issue. Raw code only — do NOT wrap in backticks or markdown fences.",
                            },
                            "agent_prompt": {
                                "type": ["string", "null"],
                                "description": "Concise imperative instruction for AI coding agents.",
                            },
                        },
                        "required": [
                            "path",
                            "line",
                            "severity",
                            "category",
                            "title",
                            "body",
                            "confidence",
                            "existing_code",
                        ],
                    },
                },
                "key_issues": {
                    "type": "array",
                    "description": "1-3 most critical findings a human reviewer MUST examine.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "issue": {"type": "string"},
                            "path": {"type": "string"},
                            "line": {"type": "integer"},
                        },
                        "required": ["issue", "path", "line"],
                    },
                },
                "summary": {
                    "type": "string",
                    "description": "Brief overall summary of the review.",
                },
                "metadata": {
                    "type": "object",
                    "properties": {
                        "reviewed_files": {"type": "integer"},
                        "skipped_reason": {"type": ["string", "null"]},
                    },
                },
            },
            "required": ["comments", "summary"],
        },
    },
}

SUBMIT_CRITIQUE_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_critique",
        "description": (
            "For each draft review comment, grade how well the evidence in "
            "the diff supports the claimed issue."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "verdicts": {
                    "type": "array",
                    "description": "One verdict per draft comment, in input order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {
                                "type": "integer",
                                "description": "Zero-based index of the draft comment.",
                            },
                            "evidence": {
                                "type": "string",
                                "enum": ["proven", "plausible", "unsupported"],
                                "description": (
                                    "proven: the shown code demonstrates the issue and the "
                                    "reasoning is correct. "
                                    "plausible: the issue is consistent with the shown code "
                                    "but depends on behaviour or code not shown — this is a "
                                    "valid grade for real findings, not a failure. "
                                    "unsupported: the shown code contradicts the claim, the "
                                    "reasoning is wrong, or it's a style preference dressed "
                                    "up as an issue."
                                ),
                            },
                            "reason": {
                                "type": "string",
                                "description": "One short sentence explaining the verdict.",
                            },
                            "settle_with": {
                                "type": "object",
                                "description": (
                                    "For plausible only: the repository file whose code "
                                    "would settle the question, and the symbol to look at "
                                    "in it. It is fetched and the comment graded again."
                                ),
                                "properties": {
                                    "path": {"type": "string"},
                                    "symbol": {"type": "string"},
                                },
                            },
                        },
                        "required": ["index", "evidence", "reason"],
                    },
                },
            },
            "required": ["verdicts"],
        },
    },
}


SUBMIT_THREAD_REPLY_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_thread_reply",
        "description": (
            "Reply to a human's comment on one of your previous PR review "
            "suggestions. Classify their intent and write a short reply."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": ["disagreement", "fixed", "question", "agreement", "other"],
                    "description": (
                        "disagreement = the human says the finding is WRONG: it does not "
                        "apply, the premise is mistaken, the code already handles it. "
                        "Only when they are refuting the finding itself. "
                        "fixed = the human accepts the finding and says they have changed "
                        'the code ("fixed", "done", "good catch, addressed below", '
                        '"corrigido"). They are agreeing with it, not refuting it. '
                        "question = the human is asking for clarification. "
                        "agreement = the human accepts the point but has not said they "
                        "changed anything yet. "
                        "other = anything else (off-topic, unclear)."
                    ),
                },
                "reply": {
                    "type": "string",
                    "description": (
                        "Your reply, 1-2 short sentences, plain text, no markdown. "
                        'No emojis, no apologies, no "as an AI". For disagreement, '
                        "concede gracefully. For questions, answer directly."
                    ),
                },
                "learning": {
                    "type": ["object", "null"],
                    "description": (
                        "For disagreement only, an explicit reusable rule proposal derived "
                        "from the original finding and the human's explanation. Null for all "
                        "other intents. This is a pending candidate, never an active rule."
                    ),
                    "properties": {
                        "rule": {
                            "type": "string",
                            "description": (
                                "A concise instruction that prevents this false-positive "
                                "pattern without suppressing genuine defects."
                            ),
                        },
                        "rationale": {
                            "type": "string",
                            "description": "Why the human feedback supports this rule.",
                        },
                        "scope_type": {
                            "type": "string",
                            "enum": ["symbol", "path", "language", "repo"],
                        },
                        "scope_value": {"type": "string"},
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                    },
                    "required": [
                        "rule",
                        "rationale",
                        "scope_type",
                        "scope_value",
                        "confidence",
                    ],
                },
            },
            "required": ["intent", "reply", "learning"],
        },
    },
}


# Used only when a human says they have fixed a finding. Their word is the
# trigger for looking, not the answer: this is the second, cheap call that
# reads the code as it stands now and decides whether the thread may close.
SUBMIT_FINDING_RECHECK_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_finding_recheck",
        "description": (
            "Decide whether a previous review finding is still present in the "
            "code as it now stands, after the author said they addressed it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "resolved": {
                    "type": "boolean",
                    "description": (
                        "True only if the code shown clearly no longer has the problem "
                        "the finding described. If the relevant code is not visible, or "
                        "the change is unrelated to the finding, answer false — an "
                        "unverified claim is not a resolution."
                    ),
                },
                "reply": {
                    "type": "string",
                    "description": (
                        "1-2 short sentences, plain text, no markdown, no emojis. If "
                        "resolved, confirm what changed. If not, say plainly what still "
                        "looks unaddressed so the author can push again."
                    ),
                },
            },
            "required": ["resolved", "reply"],
        },
    },
}


LEARNING_VERDICTS = (
    "premise_wrong",
    "fixed",
    "accepted_risk",
    "out_of_scope",
    "unclear",
)

SUBMIT_LEARNING_VERDICT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_learning_verdict",
        "description": (
            "Decide what a human's pushback on a review finding actually means, "
            "and whether it teaches a reusable rule."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": list(LEARNING_VERDICTS),
                    "description": (
                        "premise_wrong = the finding was never true of the code it "
                        "was raised on: the code already handled it at that commit, "
                        "the pattern is intentional, or the reviewer misread it. "
                        "fixed = the finding was right and the code was changed, even "
                        "if differently from the suggestion. accepted_risk = the "
                        "finding is real but the team accepts it (documented decision, "
                        "platform limitation, won't fix). out_of_scope = real but "
                        "deferred to another PR or issue. unclear = the evidence does "
                        "not settle it."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "One sentence: the evidence that decides the verdict.",
                },
                "rule": {
                    "type": "string",
                    "description": (
                        "Only for premise_wrong, otherwise empty. One imperative "
                        "sentence a reviewer can apply to OTHER code: name the code "
                        "pattern and what makes it safe, not this file or this PR. "
                        "It must still let a genuine defect of the same kind through."
                    ),
                },
                "scope_type": {
                    "type": "string",
                    "enum": ["path", "language", "repo"],
                    "description": (
                        "Where the rule holds: path for something specific to this "
                        "file, language for a language idiom, repo for a convention "
                        "of this repository."
                    ),
                },
                "confidence": {
                    "type": "number",
                    "description": "0-1 confidence in the verdict itself.",
                },
                "reply": {
                    "type": "string",
                    "description": (
                        "1-2 short sentences to the human, plain text, in the SAME "
                        "language they wrote in. Say what you concluded and why."
                    ),
                },
            },
            "required": ["verdict", "reason", "confidence", "reply"],
        },
    },
}


SUBMIT_WALKTHROUGH_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_walkthrough",
        "description": "Submit a high-level walkthrough summary of the pull request.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Brief overall summary of the PR."},
                "confidence_score": {
                    "type": "object",
                    "properties": {
                        "score": {"type": "integer", "minimum": 1, "maximum": 5},
                        "label": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["score", "label", "reason"],
                },
                "change_groups": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "files": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "path": {"type": "string"},
                                        "change_type": {
                                            "type": "string",
                                            "enum": ["added", "modified", "deleted", "renamed"],
                                        },
                                        "description": {"type": "string"},
                                    },
                                    "required": ["path", "change_type", "description"],
                                },
                            },
                        },
                        "required": ["label", "files"],
                    },
                },
                "effort": {
                    "type": "object",
                    "properties": {
                        "level": {"type": "integer", "minimum": 1, "maximum": 5},
                        "label": {"type": "string"},
                        "minutes": {"type": "integer"},
                    },
                    "required": ["level", "label", "minutes"],
                },
                "sequence_diagram": {
                    "type": ["string", "null"],
                    "description": "Mermaid sequence diagram or null.",
                },
            },
            "required": ["summary", "change_groups"],
        },
    },
}
