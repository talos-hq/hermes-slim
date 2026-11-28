"""Visible-text leak patterns used by ``strip_think_blocks``: reasoning blocks, text-channel
tool-call markup (including DeepSeek DSML) and imitated tool-history projections (#181)."""

from __future__ import annotations

import re

from agent.think_scrubber import THINK_TAG_NAMES

_TOOL_CALL_TAG_NAMES = (
    "tool_call", "tool_calls", "tool_result", "function_call", "function_calls",
    # Bare invoke/parameter/calls blocks: DeepSeek (and routes that proxy it) serialize a native
    # call onto the text channel as <tool_calls><invoke><parameter/> blocks.
    "invoke", "parameter", "calls",
)
# Optional XML namespace prefix: some models serialize native tool calls as <ns:function_calls>.
_NS_PREFIX = r"(?:[\w.-]+:)?"
# DeepSeek's DSML text-channel call syntax prefixes every tag with its marker token:
#   <{marker}invoke name="x"><{marker}parameter name="y">v<{marker}/parameter></{marker}invoke>
# The marker (an angle bracket char 0xFF5C plus a zero-width space) sits INSIDE the
# the tag-name patterns above never match and the markup leaked to users verbatim. Normalize the
# marker away first; the ordinary tag patterns then strip the block like any other leak.
_DSML_TAG_PREFIX_PATTERN = re.compile(
    r"<(\s*/)?\s*(?:\uff5c\ufe0f?\u200b?\s*DSML\ufe0f?\u200b?\s*\uff5c)+\s*", re.IGNORECASE
)


def normalize_dsml_tags(text: str) -> str:
    """Rewrite a DSML-prefixed open/close tag (marker inside the angle brackets) as a plain tag."""
    if not text or "DSML" not in text:
        return text
    return _DSML_TAG_PREFIX_PATTERN.sub(lambda m: "</" if m.group(1) else "<", text)


_REASONING_BLOCK_PATTERNS = tuple(
    re.compile(rf"<{name}>.*?</{name}>", re.DOTALL | re.IGNORECASE) for name in THINK_TAG_NAMES
)
_TOOL_CALL_BLOCK_PATTERNS = tuple(
    re.compile(rf"<{_NS_PREFIX}{name}\b[^>]*>.*?</{_NS_PREFIX}{name}>", re.DOTALL | re.IGNORECASE)
    for name in _TOOL_CALL_TAG_NAMES
)

# Named <function name=...> blocks; boundary- and name-gated (see _THINK_STRIP_PATTERNS note).
_NAMED_FUNCTION_BLOCK_PATTERN = re.compile(
    r'(?:(?<=^)|(?<=[\n\r.!?:]))[ \t]*'
    r'<function\b[^>]*\bname\s*=[^>]*>'
    r'(?:(?:(?!</function>).)*)</function>', re.DOTALL | re.IGNORECASE,
)
_UNTERMINATED_REASONING_BLOCK_PATTERN = re.compile(
    rf'(?:^|\n)[ \t]*<(?:{"|".join(THINK_TAG_NAMES)})\b[^>]*>.*$', re.DOTALL | re.IGNORECASE
)
_ORPHAN_REASONING_TAG_PATTERN = re.compile(
    rf'</?(?:{"|".join(THINK_TAG_NAMES)})>\s*', re.IGNORECASE
)
_STRAY_TOOL_CALL_CLOSER_PATTERN = re.compile(
    rf'</(?:{_NS_PREFIX}(?:{"|".join(_TOOL_CALL_TAG_NAMES)}|function))>\s*', re.IGNORECASE
)
# Synthetic tool-history projection (agent.turn_api_request.flatten_replayed_tool_history renders a
# rejected OpenRouter tool round as plain text). It is REQUEST-ONLY, but a model that has seen it
# imitates the shape and writes "<note> Historical result from terminal (untrusted tool output): > …"
# into its own reply, which then reaches the user verbatim. Strip the markers from visible text.
# Models paraphrase the note ("[Historical tool call output rendered compactly.]",
# "[Historical …]"), so any short bracketed note that opens with "Historical" counts (#181).
# The header of the current user-role projection is matched too, in case a model echoes it.
_HISTORICAL_HISTORY_NOTE_PATTERN = re.compile(
    r"\[\s*Historical\b[^\]\n]{0,160}\]\s*"
    r"|\(\s*Context from Hermes, not written by the assistant\b[^)\n]*\)\s*",
    re.IGNORECASE,
)
_HISTORICAL_TOOL_RESULT_PATTERN = re.compile(
    r"Historical result from [\w.:-]+[^\n]*(?:\n>[^\n]*)*\n?",
    re.IGNORECASE,
)
# Catch-all for mangled imitations the two patterns above cannot parse (truncated, missing the
# "(untrusted tool output)" tail, doubled notes). Line-start gated so ordinary prose survives.
_HISTORICAL_STRAY_LINE_PATTERN = re.compile(
    r"(?:^|\n)[ \t]*[\[(]?[ \t]*Historical[ \t]+(?:result|calls?|results?)[^\n]*", re.IGNORECASE
)

# An unclosed tool call is unrecoverable (#101899), so drop its remaining block.
# Stray argument tags only identify fragment lines, not the rest of the text
# (#102303). Require a line-start tag (optionally glued to a bare tool name,
# process_manage<arg_key>) or a line-ending closer (wait</arg_value>) so inline
# prose mentions and subsequent prose survive.
_UNTERMINATED_TOOL_CALL_PATTERN = re.compile(
    rf'(?:^|\n)[ \t]*<{_NS_PREFIX}(?:{"|".join(_TOOL_CALL_TAG_NAMES)})\b[^>]*>.*$'
    r'|(?:^|\n)[ \t]*[\w.:-]*</?arg_(?:key|value)\b[^\n]*'
    r'|(?:^|\n)[^\n<]*</arg_(?:key|value)>[ \t\r]*(?=\n|$)',
    re.DOTALL | re.IGNORECASE,
)


# Order matters: closed pairs first (case-insensitive so mixed-case tags don't fall through to the
# unterminated pass and eat trailing content), then tool-call XML blocks, the boundary+name-gated
# <function> block, the unterminated reasoning block, stray orphan reasoning tags, and finally stray
# tool-call CLOSERS only (bare/unterminated <function> is kept: a truncated streaming tail may still
# be valuable, matching OpenClaw's asymmetry).
_THINK_STRIP_PATTERNS = (
    *_REASONING_BLOCK_PATTERNS, *_TOOL_CALL_BLOCK_PATTERNS, _NAMED_FUNCTION_BLOCK_PATTERN,
    _UNTERMINATED_REASONING_BLOCK_PATTERN, _ORPHAN_REASONING_TAG_PATTERN,
    _STRAY_TOOL_CALL_CLOSER_PATTERN, _UNTERMINATED_TOOL_CALL_PATTERN,
    _HISTORICAL_HISTORY_NOTE_PATTERN, _HISTORICAL_TOOL_RESULT_PATTERN,
    _HISTORICAL_STRAY_LINE_PATTERN,
)
