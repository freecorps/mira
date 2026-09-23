"""Line numbers the model reads, and the lines its comments land on."""

from __future__ import annotations

from mira.core.context import (
    build_file_context_string,
    expand_context,
    number_hunk_lines,
    strip_line_gutter,
)
from mira.core.diff_parser import parse_diff
from mira.core.enclosing import build_enclosing_context
from mira.llm.response_parser import (
    LLMComment,
    LLMReviewResponse,
    convert_to_review_comments,
)
from mira.models import FileChangeType, FileDiff, HunkInfo

_DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -10,6 +10,7 @@ def handler(request):
     user = request.user
     if not user:
         return None
-    total = compute(user.items)
+    items = user.items or []
+    total = compute(items)
     save(total)
     return total
@@ -40,3 +41,4 @@ def other():
     a = 1
     b = 2
+    c = a + b
     return c
"""


def _file() -> FileDiff:
    return expand_context(parse_diff(_DIFF).files)[0]


def _convert(line: int, quote: str, end_line: int | None = None) -> list[tuple[int, int | None]]:
    response = LLMReviewResponse(
        comments=[
            LLMComment(
                path="app.py",
                line=line,
                end_line=end_line,
                title="t",
                body="b",
                confidence=0.9,
                existing_code=quote,
            )
        ]
    )
    return [
        (c.line, c.end_line) for c in convert_to_review_comments(response, {"app.py"}, [_file()])
    ]


class TestGutter:
    def test_every_new_side_line_carries_its_number(self):
        text = number_hunk_lines(_file().hunks[0])
        lines = text.splitlines()
        assert lines[0].startswith("@@ -10,6 +10,7 @@")
        assert lines[1] == "   10      user = request.user"
        assert lines[4] == "      -    total = compute(user.items)"
        assert lines[5] == "   13 +    items = user.items or []"
        assert lines[6] == "   14 +    total = compute(items)"

    def test_merged_hunks_restart_at_their_own_header(self):
        merged = HunkInfo(
            1,
            2,
            1,
            2,
            "@@ -1,1 +1,1 @@\n a\n\n@@ -30,1 +31,1 @@\n b\n",
        )
        text = number_hunk_lines(merged)
        assert "   31  b" in text
        # The seam between merged hunks is not a line of the file.
        assert "    2 " not in text

    def test_only_newlines_break_lines(self):
        """splitlines() also breaks at form feeds and U+2028; the platform does not."""
        content = "@@ -1,3 +1,3 @@\n a = '\u2028'\n \x0c\n+c = 1\n"
        hunk = HunkInfo(1, 3, 1, 3, content)
        assert "    3 +c = 1" in number_hunk_lines(hunk)

    def test_prompt_renders_the_gutter(self):
        assert "   14 +    total = compute(items)" in build_file_context_string(_file())
        assert "+    total = compute(items)" in build_file_context_string(_file(), numbered=False)

    def test_gutter_copied_into_a_quote_is_taken_off(self):
        assert (
            strip_line_gutter("   14 +    total = compute(items)") == "    total = compute(items)"
        )
        assert strip_line_gutter("+x = 1\n-y = 2") == "x = 1\ny = 2"
        # Not a marker: code that happens to start with the same character.
        assert strip_line_gutter("++i") == "++i"


class TestAnchoring:
    def test_right_line_and_quote_stand(self):
        assert _convert(14, "total = compute(items)") == [(14, None)]

    def test_miscounted_line_moves_to_the_quoted_code(self):
        # 30 is in neither hunk and too far from both to be snapped back.
        assert _convert(30, "total = compute(items)") == [(14, None)]

    def test_quote_with_the_gutter_still_matches(self):
        assert _convert(13, "   14 +    total = compute(items)") == [(14, None)]

    def test_whitespace_mangled_quote_still_matches(self):
        assert _convert(14, "total  =   compute(items)") == [(14, None)]

    def test_quote_of_removed_code_lands_where_it_was(self):
        assert _convert(13, "total = compute(user.items)") == [(13, None)]

    def test_multi_line_quote(self):
        quote = "items = user.items or []\ntotal = compute(items)"
        assert _convert(40, quote) == [(13, 14)]

    def test_short_ambiguous_quote_does_not_overrule_a_line_in_the_diff(self):
        assert _convert(43, "c") == [(43, None)]

    def test_quote_that_is_not_in_the_diff_is_dropped(self):
        assert _convert(14, "totally_made_up(call)") == []


_SOURCE = "\n".join(
    [
        "import os",
        "",
        "def handler(request):",
        '    """Handle it."""',
        "    user = request.user",
    ]
    + [f"    step_{i} = {i}" for i in range(20)]
    + [
        "    total = compute(user.items)",
        "    save(total)",
        "    return total",
        "",
        "def untouched():",
        "    return 1",
    ]
)


class _Fetcher:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    async def fetch(self, path: str) -> str | None:
        return self.files.get(path)


def _changed_at(line: int, change_type: FileChangeType = FileChangeType.MODIFIED) -> FileDiff:
    return FileDiff(
        path="app.py",
        change_type=change_type,
        hunks=[HunkInfo(line, 1, line, 1, f"@@ -{line},1 +{line},1 @@\n-    old\n+    new\n")],
        language="python",
    )


class TestEnclosingContext:
    async def test_whole_function_around_the_change(self):
        out = await build_enclosing_context(
            [_changed_at(26)], _Fetcher({"app.py": _SOURCE}), 10_000
        )
        assert "`handler` (lines 3-" in out
        assert "   26      total = compute(user.items)" in out
        assert "untouched" not in out

    async def test_added_files_are_left_to_the_diff(self):
        out = await build_enclosing_context(
            [_changed_at(26, FileChangeType.ADDED)], _Fetcher({"app.py": _SOURCE}), 10_000
        )
        assert out == ""

    async def test_budget_is_respected(self):
        out = await build_enclosing_context([_changed_at(26)], _Fetcher({"app.py": _SOURCE}), 100)
        assert out == ""

    async def test_no_source_no_context(self):
        assert await build_enclosing_context([_changed_at(26)], None, 10_000) == ""
        assert await build_enclosing_context([_changed_at(26)], _Fetcher({}), 10_000) == ""
