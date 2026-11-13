"""Desktop/TUI wire for workflows: workflow.list/save RPCs and /workflow via command.dispatch."""
from tui_gateway import server

SOURCE = "description: d\nsteps:\n  - name: One\n    prompt: Do {input}.\n"


def _call(method, **params):
    return server.handle_request({"jsonrpc": "2.0", "id": "r1", "method": method, "params": params})


def test_save_then_list_round_trips_source():
    saved = _call("workflow.save", name="demo", source=SOURCE)
    assert saved["result"] == {"name": "demo", "steps": 1}
    listed = _call("workflow.list")["result"]
    assert [(w["name"], w["source"]) for w in listed["workflows"]] == [("demo", SOURCE)]


def test_invalid_save_is_an_error_and_lists_nothing():
    assert _call("workflow.save", name="demo", source="steps: []")["error"]["code"] == 4004
    assert _call("workflow.list")["result"]["workflows"] == []


def test_slash_workflow_dispatches_a_send_turn():
    _call("workflow.save", name="demo", source=SOURCE)
    res = _call("command.dispatch", name="workflow", arg="demo the thing")["result"]
    assert res["type"] == "send" and "Do the thing." in res["message"]
    missing = _call("command.dispatch", name="workflow", arg="nope")["result"]
    assert missing["type"] == "exec" and "usage: /workflow" in missing["output"]
