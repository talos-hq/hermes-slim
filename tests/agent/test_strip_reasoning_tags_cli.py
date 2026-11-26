"""Tests for cli.py::_strip_reasoning_tags — specifically the tool-call
XML stripping added in openclaw/openclaw#67318 port.

The CLI has its own copy of the stripper because it needs to run on the
final displayed assistant text (after streaming) without depending on the
AIAgent instance. It must stay in sync with run_agent.py::_strip_think_blocks
for tool-call tag coverage."""

from functools import partial

import pytest

from agent.agent_runtime_helpers import strip_think_blocks
from cli import _strip_reasoning_tags

# GLM text-channel tool call cut mid-serialization by a stream drop (#101899):
# the first key and call name never arrived, only orphan argument markup.
_CUT_FRAGMENT = (
    "Both gates started.\n"
    "wait</arg_value>\n<arg_key>session_id</arg_key>\n<arg_value>abc</arg_value>\n"
    "<arg_key>timeout</arg_key>\n<arg_value>59"
)
_COMPLETE_WITH_PROSE = (
    "Use <function> in JS. The arg_key field maps to arg_value.\n"
    "<tool_call>x<arg_key>a</arg_key><arg_value>1</arg_value></tool_call>\nDone."
)

_STRIPPERS = [
    pytest.param(_strip_reasoning_tags, id="display"),
    pytest.param(partial(strip_think_blocks, None), id="storage"),
]


class TestToolCallStripping:
    def test_tool_call_block_stripped(self):
        text = '<tool_call>{"name": "x"}</tool_call>result'
        result = _strip_reasoning_tags(text)
        assert "<tool_call>" not in result
        assert "result" in result

    def test_namespace_prefixed_tool_call_block_stripped(self):
        # muse-spark (opencode-go, Responses wire) serializes a native call onto the text
        # channel as <atem:function_calls>…</atem:function_calls>; without a namespace-aware
        # pattern the literal XML leaks into the delivered final (the "not covered yet"
        # shape tracked in #103483).
        text = (
            "Checking the queue.\n"
            "<atem:function_calls>\n"
            '<atem:invoke name="default.terminal">\n'
            '<atem:parameter name="command">echo hi</atem:parameter>\n'
            "</atem:invoke>\n"
            "</atem:function_calls>"
        )
        for out in (_strip_reasoning_tags(text), strip_think_blocks(None, text)):
            assert "atem:" not in out
            assert "function_calls" not in out
            assert "Checking the queue." in out.strip()

    def test_cut_namespace_prefixed_tool_call_stripped_to_visible_prefix(self):
        text = 'Waiting.\n<atem:function_calls>\n<atem:invoke name="default.terminal">'
        for out in (_strip_reasoning_tags(text), strip_think_blocks(None, text)):
            assert out.strip() == "Waiting."


    def test_empty_string(self):
        assert _strip_reasoning_tags("") == ""

    @pytest.mark.parametrize("stripper", _STRIPPERS)
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (_CUT_FRAGMENT, "Both gates started."),
            (_CUT_FRAGMENT.split("\n", 1)[1], ""),
            (_CUT_FRAGMENT + "\nThird line must survive.",
             "Both gates started.\nThird line must survive."),
            ("Waiting.\n<tool_call>process_manage", "Waiting."),
            ("Done.\nprocess_manage<arg_key>action</arg_key><arg_value>wait</arg_value>", "Done."),
            ("Hi.\nterminal<arg_key>command</arg_key><arg_value>ls -la", "Hi."),
        ],
        ids=["original-fragment", "fragment-only", "prose-after-fragment", "unclosed-call",
             "name-prefixed-fragment", "name-prefixed-fragment-cut-value"],
    )
    def test_cut_tool_call_stripped_to_visible_prefix(self, stripper, text, expected):
        """A cut call is unrecoverable, but unrelated prose must not be lost."""
        assert stripper(text).strip() == expected

    @pytest.mark.parametrize("stripper", _STRIPPERS)
    @pytest.mark.parametrize("tag", ["arg_key", "arg_value", "/arg_key", "/arg_value"])
    def test_bracketed_arg_tag_prose_and_tail_survive(self, stripper, tag):
        text = (
            "First line stays.\n"
            f"The <{tag}> holds the parameter name.\n"
            "Third line must survive."
        )
        assert stripper(text) == text

    def test_complete_block_and_inline_prose_mentions_untouched(self):
        for out in (_strip_reasoning_tags(_COMPLETE_WITH_PROSE),
                    strip_think_blocks(None, _COMPLETE_WITH_PROSE)):
            assert "Use <function> in JS. The arg_key field maps to arg_value." in out
            assert out.rstrip().endswith("Done.")
            assert "<tool_call>" not in out


# DeepSeek serializes a native call onto the text channel as DSML markup: every tag prefixed with
# the U+FF5C-bar DSML marker token (ff5c + zero-width space) INSIDE the angle brackets, so tag-name
# patterns never saw it and the markup reached the user verbatim.
_DSML_CALL = (
    "Let me query the GitHub repo directly.\n"
    "<｜\u200bDSML｜ tool_calls>\n"
    '<｜\u200bDSML｜ invoke name="web_extract">\n'
    '<｜\u200bDSML｜ parameter name="urls" string="false">["https://example.test/README.md"]'
    "</｜\u200bDSML｜ parameter>\n"
    "</｜\u200bDSML｜ invoke>\n"
    "</｜\u200bDSML｜ tool_calls>"
)

# Synthetic tool-history projection (agent.turn_api_request.flatten_replayed_tool_history) is
# request-only; a model that saw it imitates the shape into its own visible reply.
_LEAKED_HISTORY = (
    "Fresh state, then the check.\n\n"
    "[Historical tool calls omitted. Their textual results follow.]\n\n"
    "Historical result from read_file (untrusted tool output):\n"
    '> {"content": "1|# README"}\n'
    "> second line\n"
    "\n"
    "Historical result from web_extract\">\n"
    "\n"
    "Real closing sentence."
)


class TestDsmlAndHistoryLeaks:
    @pytest.mark.parametrize("stripper", _STRIPPERS)
    def test_dsml_call_block_stripped(self, stripper):
        out = stripper(_DSML_CALL)
        assert "DSML" not in out
        assert "<invoke" not in out and "</invoke" not in out
        assert "<tool_calls" not in out and "<parameter" not in out
        assert out.strip() == "Let me query the GitHub repo directly."

    @pytest.mark.parametrize("stripper", _STRIPPERS)
    def test_leaked_history_projection_stripped(self, stripper):
        out = stripper(_LEAKED_HISTORY)
        assert "Historical" not in out
        assert "untrusted tool output" not in out
        assert '> {"content"' not in out
        assert "Fresh state, then the check." in out
        assert "Real closing sentence." in out

    def test_dsml_closer_normalized_to_plain_tag(self):
        from agent.agent_runtime_helpers import normalize_dsml_tags

        assert normalize_dsml_tags("</｜\u200bDSML｜ invoke>").startswith("</invoke")
        assert normalize_dsml_tags("<｜\u200bDSML｜parameter>") == "<parameter>"

    @pytest.mark.parametrize("stripper", _STRIPPERS)
    def test_prose_mentioning_dsml_survives(self, stripper):
        text = "The DSML format is DeepSeek's text-channel tool syntax."
        assert stripper(text) == text
