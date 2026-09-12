"""Pure graph evaluation; no LLM, code execution or provider writes."""

from fnmatch import fnmatchcase

from mira.labels.models import Condition, Evaluation, LabelWorkflow, PreviewResponse, PRFacts


def matches(condition: Condition, facts: PRFacts) -> bool:
    actual = getattr(facts, condition.field)
    expected = condition.value
    op = condition.operator
    if isinstance(expected, str):
        values = actual if isinstance(actual, list) else [actual]
        if condition.field == "author":
            values = [value.lstrip("@").casefold() for value in values]
            expected = expected.lstrip("@").casefold()
        if op == "ne":
            return all(value != expected for value in values)
        return any(
            value == expected
            if op == "eq"
            else expected in value
            if op == "contains"
            else value.startswith(expected)
            if op == "starts_with"
            else fnmatchcase(value, expected)
            for value in values
        )
    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    if op == "gt":
        return actual > expected
    if op == "gte":
        return actual >= expected
    if op == "lt":
        return actual < expected
    return actual <= expected


def evaluate(workflow: LabelWorkflow, facts: PRFacts) -> Evaluation:
    """A node is reached by ANY incoming path; chained conditions express AND.

    Preview deliberately evaluates disabled workflows so drafts can be tested.
    """
    by_id = {node.id: node for node in workflow.nodes}
    pending = [node.id for node in workflow.nodes if node.kind == "start"]
    visited: set[str] = set()
    labels = {}
    while pending:
        key = pending.pop()
        if key in visited:
            continue
        visited.add(key)
        node = by_id[key]
        branch = "next"
        if node.condition:
            branch = "true" if matches(node.condition, facts) else "false"
        if node.action:
            labels[node.action.name.casefold()] = node.action
        pending.extend(e.target for e in workflow.edges if e.source == key and e.branch == branch)
    return Evaluation(
        matched_nodes=sorted(visited),
        labels=sorted(labels.values(), key=lambda label: label.name),
        managed_labels=sorted(
            {n.action.name for n in workflow.nodes if n.action and n.action.mode == "sync"}
        ),
        total_lines=facts.total_lines,
    )


def plan(
    evaluation: Evaluation, current: list[str], previous: list[str] | None = None
) -> PreviewResponse:
    desired = {label.name.casefold(): label.name for label in evaluation.labels}
    present = {name.casefold(): name for name in current}
    managed = {name.casefold() for name in [*evaluation.managed_labels, *(previous or [])]}
    # An action changed from sync to add hands ownership back to the user.
    managed -= {label.name.casefold() for label in evaluation.labels if label.mode == "add"}
    return PreviewResponse(
        evaluation=evaluation,
        add=sorted(name for key, name in desired.items() if key not in present),
        remove=sorted(
            name for key, name in present.items() if key in managed and key not in desired
        ),
    )
