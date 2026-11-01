"""cron.manage ``update`` — the Bot Mode Routines editor's write path.

Before this action the RPC only offered list/add/remove/pause/resume, so a routine's title,
instruction and schedule could not change after creation. ``list`` also carried only a
100-char ``prompt_preview``, which an editor would have saved back as a truncated prompt.
"""

import json

from tui_gateway import server

LONG_PROMPT = "Define and implement the knowledge base. " * 10


def _profile(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "botA"
    (home / "cron").mkdir(parents=True)
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{
        "id": "job-kb", "name": "[bot:botA] KB policy", "prompt": LONG_PROMPT,
        "schedule": {"kind": "interval", "minutes": 120, "display": "every 120m"},
        "schedule_display": "every 120m", "repeat": {"times": None, "completed": 0},
        "enabled": True, "state": "scheduled", "next_run_at": "2099-01-01T00:00:00+00:00",
        "deliver": "local"}]}), encoding="utf-8")
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: home)
    return home


def _call(params):
    resp = server.handle_request({"id": "1", "method": "cron.manage", "params": {"profile": "botA", **params}})
    assert "result" in resp, resp
    return resp["result"]


def _stored(home):
    return json.loads((home / "cron" / "jobs.json").read_text(encoding="utf-8"))["jobs"][0]


def test_list_carries_the_full_prompt(tmp_path, monkeypatch):
    _profile(tmp_path, monkeypatch)
    row = _call({"action": "list", "include_disabled": True})["jobs"][0]
    assert row["prompt"] == LONG_PROMPT
    assert len(row["prompt_preview"]) < len(LONG_PROMPT)


def test_update_persists_every_editable_field(tmp_path, monkeypatch):
    home = _profile(tmp_path, monkeypatch)
    result = _call({
        "action": "update", "job_id": "job-kb", "name": "[bot:botA] KB policy v2",
        "prompt": "new body", "schedule": "0 9 * * 1-5", "repeat": 3, "model": "m-1",
        "workdir": str(tmp_path), "skills": ["alpha", " "], "enabled_toolsets": ["web"],
        "continuity": True, "reasoning_effort": "low"})
    assert result.get("success") is True, result
    job = _stored(home)
    assert job["name"] == "[bot:botA] KB policy v2"
    assert job["prompt"] == "new body"
    assert job["schedule_display"] == "0 9 * * 1-5"
    assert job["repeat"]["times"] == 3
    assert job["model"] == "m-1"
    assert job["skills"] == ["alpha"]
    assert job["enabled_toolsets"] == ["web"]
    assert job["context_from"] == ["self"] or job["context_from"] == ["job-kb"]
    assert job["reasoning_effort"] == "low"


def test_prompt_only_update_keeps_schedule_and_next_run(tmp_path, monkeypatch):
    home = _profile(tmp_path, monkeypatch)
    _call({"action": "update", "job_id": "job-kb", "prompt": "only this", "schedule": "  "})
    job = _stored(home)
    assert job["prompt"] == "only this"
    assert job["schedule_display"] == "every 120m"
    assert job["next_run_at"] == "2099-01-01T00:00:00+00:00"
    assert job["name"] == "[bot:botA] KB policy"
