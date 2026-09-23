"""Tool calls written out in the Qwen/Hermes XML form instead of JSON."""

from __future__ import annotations

import json

from mira.core.passes import _review_json_in
from mira.llm.base import _as_json_object
from mira.llm.utils import parse_xml_tool_call

# As seen from MiMo on OpenCode Go, which cost a walkthrough three re-rolls.
_WALKTHROUGH = (
    "<tool_call><function=submit_walkthrough><parameter=summary>This PR makes "
    "structured output conform to its schema.</parameter><parameter=effort>"
    '{"level": 2, "label": "Easy", "minutes": 10}</parameter><parameter=change_groups>'
    '[{"label": "LLM", "files": []}]</parameter></function></tool_call>'
)


class TestParse:
    def test_reads_name_and_typed_arguments(self):
        name, args = parse_xml_tool_call(_WALKTHROUGH)
        assert name == "submit_walkthrough"
        assert args["summary"].startswith("This PR makes")
        assert args["effort"] == {"level": 2, "label": "Easy", "minutes": 10}
        assert args["change_groups"] == [{"label": "LLM", "files": []}]

    def test_unclosed_parameters_and_scalars(self):
        name, args = parse_xml_tool_call(
            "<function=submit_review>\n<parameter=comments>\n[]\n"
            "<parameter=reviewed>\n3\n<parameter=skipped>\nnull\n<parameter=summary>\nclean"
        )
        assert name == "submit_review"
        assert args == {"comments": [], "reviewed": 3, "skipped": None, "summary": "clean"}

    def test_number_like_strings_and_trailing_tags(self):
        _, args = parse_xml_tool_call(
            "<function=f><parameter=code>007<parameter=summary>done</function></tool_call>"
        )
        assert args == {"code": "007", "summary": "done"}

    def test_json_is_not_mistaken_for_xml(self):
        assert parse_xml_tool_call('{"comments": []}') is None


class TestReaders:
    def test_tool_arguments_in_xml_are_accepted(self):
        payload = _as_json_object(_WALKTHROUGH)
        assert payload is not None
        assert json.loads(payload)["effort"]["level"] == 2

    def test_review_written_as_an_xml_call_in_the_reply(self):
        reply = (
            "<tool_call><function=submit_review><parameter=comments>[]</parameter>"
            "<parameter=summary>looks fine</parameter></function></tool_call>"
        )
        review = _review_json_in(reply)
        assert review is not None and json.loads(review)["summary"] == "looks fine"

    def test_xml_call_to_another_tool_is_not_a_review(self):
        reply = (
            "<tool_call><function=read_file><parameter=path>a.py</parameter>"
            "<parameter=comments>[]</parameter></function></tool_call>"
        )
        assert _review_json_in(reply) is None
