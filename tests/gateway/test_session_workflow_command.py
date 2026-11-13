"""/workflow on a local (desktop) session of the native gateway: admitted, rewritten to the workflow
prompt in the session's profile, and an unknown name answers usage instead of starting a turn."""
import asyncio
from types import SimpleNamespace

from agent import workflow as wf


def test_local_session_admits_workflow_as_a_prompt_command():
    from gateway.session_commands import command_verdict
    assert command_verdict('workflow', 'demo some input') == 'control'


def _run(text):
    from gateway.run_inbound import GatewayInboundMixin
    acks = []

    async def ack(source, text, name):
        acks.append(text)
    runner = SimpleNamespace(_send_command_ack=ack)
    runner._hm_rewrite_turn_to_prompt = (
        lambda *a: GatewayInboundMixin._hm_rewrite_turn_to_prompt(runner, *a))
    event = SimpleNamespace(text=text, get_command_args=lambda: text.split(None, 1)[1] if ' ' in text else '')
    result = asyncio.run(GatewayInboundMixin._hm_cmd_workflow(runner, event, None, None))
    return result, event.text, acks


def test_workflow_rewrites_the_turn_to_the_saved_steps():
    wf.save_workflow('demo', 'steps:\n  - name: One\n    prompt: Do {input}.\n')
    (handled, _), text, acks = _run('/workflow demo the thing')
    assert handled is False and '[/workflow demo]' in text and 'Do the thing.' in text
    assert acks and 'demo' in acks[0]


def test_unknown_workflow_replies_usage_and_starts_no_turn():
    (handled, reply), text, acks = _run('/workflow nope')
    assert handled is True and 'usage: /workflow' in reply
    assert text == '/workflow nope' and acks == []
