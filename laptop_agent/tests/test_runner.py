import dataclasses
import time
import uuid
from datetime import UTC, datetime

import pytest
from conftest import make_config

from laptop_agent.runner import Agent
from laptop_agent.signing import sign

SECRET = "test-secret-0123456789abcdef0123456789"
NOW = 1_900_000_000.0


class FakeDB:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.finished: list[dict] = []
        self.heartbeats: list[dict] = []
        self.claims = 0

    async def claim(self):
        self.claims += 1
        return self.rows.pop(0) if self.rows else None

    async def finish(self, command_id, status, result, error, audit):
        self.finished.append(
            dict(id=command_id, status=status, result=result, error=error, audit=audit)
        )
        return True

    async def heartbeat(self, state):
        self.heartbeats.append(state)


def make_row(tool, args=None, *, status="pending", expires_in=60, secret=SECRET, cid=None):
    cid = cid or str(uuid.uuid4())
    args = args or {}
    expires = int(NOW + expires_in)
    return {
        "id": cid,
        "tool": tool,
        "args": args,
        "status": "running",
        "claimed_from": status,
        "expires_at": datetime.fromtimestamp(expires, UTC).isoformat(),
        "signature": sign(secret, cid, tool, args, expires),
    }


def make_agent(config, tmp_path, rows=(), **kw):
    db = FakeDB(rows)
    agent = Agent(config, db, SECRET, pause_file=tmp_path / "PAUSE", clock=lambda: NOW, **kw)
    return agent, db


@pytest.fixture
def writable(home):
    return make_config(home, agent__read_only=False)


async def run_one(agent, row):
    return await agent.handle(row)


# signing, expiry, replay


async def test_valid_command_runs(config, tmp_path):
    agent, _ = make_agent(config, tmp_path)
    out = await run_one(agent, make_row("read_file", {"path": "invoice.txt"}))
    assert out.status == "done" and out.result["content"] == "Invoice 42: 100 USD"


async def test_tampered_args_rejected(config, tmp_path):
    agent, _ = make_agent(config, tmp_path)
    row = make_row("read_file", {"path": "invoice.txt"})
    row["args"] = {"path": "notes.md"}
    out = await run_one(agent, row)
    assert out.status == "rejected" and out.result["reason"] == "bad_signature"


async def test_wrong_secret_and_unsigned_rejected(config, tmp_path):
    agent, _ = make_agent(config, tmp_path)
    bad = make_row("list_dir", secret="some-other-secret-0123456789abcdefgh")
    assert (await run_one(agent, bad)).result["reason"] == "bad_signature"
    unsigned = make_row("list_dir")
    unsigned["signature"] = None
    assert (await run_one(agent, unsigned)).result["reason"] == "bad_signature"


async def test_expired_and_overlong_expiry(config, tmp_path):
    agent, _ = make_agent(config, tmp_path)
    assert (await run_one(agent, make_row("list_dir", expires_in=-1))).status == "expired"
    far = make_row("list_dir", expires_in=3600)
    assert (await run_one(agent, far)).result["reason"] == "bad_expiry"


async def test_replayed_command_rejected(config, tmp_path):
    agent, _ = make_agent(config, tmp_path)
    row = make_row("list_dir")
    assert (await run_one(agent, row)).status == "done"
    again = await run_one(agent, dict(row))
    assert again.status == "rejected" and again.result["reason"] == "replayed"


# modes


async def test_read_only_rejects_tier2_even_when_approved(config, downloads, tmp_path):
    assert config.read_only
    agent, _ = make_agent(config, tmp_path)
    for row in [
        make_row("move_file", {"src": "invoice.txt", "dest": "Invoices"}, status="approved"),
        make_row("delete_file", {"path": "invoice.txt"}, status="approved"),
    ]:
        out = await run_one(agent, row)
        assert out.status == "rejected" and out.result["reason"] == "read_only"
    assert (downloads / "invoice.txt").exists()


async def test_tier2_needs_approval_tier_from_agent_table(writable, downloads, tmp_path):
    agent, _ = make_agent(writable, tmp_path)
    row = make_row("delete_file", {"path": "invoice.txt"}, status="pending")
    row["tier"] = 0  # anything the row claims about its tier is ignored
    out = await run_one(agent, row)
    assert out.result["reason"] == "not_approved" and (downloads / "invoice.txt").exists()
    ok = await run_one(agent, make_row("delete_file", {"path": "invoice.txt"}, status="approved"))
    assert ok.status == "done" and not (downloads / "invoice.txt").exists()


async def test_dry_run_through_agent_changes_nothing(home, downloads, tmp_path):
    cfg = make_config(home, agent__read_only=False, agent__dry_run=True)
    agent, _ = make_agent(cfg, tmp_path)
    out = await run_one(
        agent, make_row("move_file", {"src": "invoice.txt", "dest": "Invoices"}, status="approved")
    )
    assert out.status == "done" and out.result["dry_run"]
    assert (downloads / "invoice.txt").exists()


async def test_pause_file_blocks_everything(config, tmp_path):
    agent, db = make_agent(config, tmp_path, rows=[make_row("list_dir")])
    (tmp_path / "PAUSE").touch()
    assert await agent.tick() is False and db.claims == 0
    out = await run_one(agent, make_row("list_dir"))
    assert out.result["reason"] == "paused"
    assert agent.state()["paused"] is True


async def test_disabled_agent_blocks_everything(home, tmp_path):
    agent, db = make_agent(make_config(home, agent__enabled=False), tmp_path, rows=[make_row("list_dir")])
    assert await agent.tick() is False and db.claims == 0


async def test_pause_command_creates_pause_file_and_cannot_resume(config, tmp_path):
    agent, _ = make_agent(config, tmp_path)
    out = await run_one(agent, make_row("pause"))
    assert out.status == "done" and (tmp_path / "PAUSE").exists()
    for tool in ["resume", "unpause"]:
        assert (await run_one(agent, make_row(tool))).result["reason"] == "unknown_tool"
    assert (tmp_path / "PAUSE").exists()


async def test_unknown_and_disabled_tools_rejected(home, tmp_path):
    cfg = make_config(home, tools__enabled=["list_dir"])
    agent, _ = make_agent(cfg, tmp_path)
    for tool in ["run_script", "write_file", "open_app", "open_url", "system_info"]:
        assert (await run_one(agent, make_row(tool))).result["reason"] == "unknown_tool"
    out = await run_one(agent, make_row("read_file", {"path": "invoice.txt"}))
    assert out.result["reason"] == "tool_disabled"


async def test_invalid_args_rejected(config, tmp_path):
    agent, _ = make_agent(config, tmp_path)
    out = await run_one(agent, make_row("read_file", {"path": "a.txt", "mode": "w"}))
    assert out.result["reason"] == "invalid_args"


async def test_sandbox_rejections_come_back_as_rejected(config, tmp_path):
    agent, _ = make_agent(config, tmp_path)
    out = await run_one(agent, make_row("read_file", {"path": "~/.ssh/id_rsa"}))
    assert out.status == "rejected" and out.result["reason"] == "outside_scope"


# limits


async def test_timeout(config, tmp_path, monkeypatch):
    cfg = dataclasses.replace(config, command_timeout_seconds=0.05)
    agent, _ = make_agent(cfg, tmp_path)
    monkeypatch.setattr(agent.tools, "list_dir", lambda path="": time.sleep(0.5))
    out = await run_one(agent, make_row("list_dir"))
    assert out.status == "failed" and out.error == "timed out"


async def test_output_truncation(config, downloads, tmp_path):
    (downloads / "long.txt").write_text("y" * 15_000)
    cfg = dataclasses.replace(config, max_output_bytes=2_000)
    agent, _ = make_agent(cfg, tmp_path)
    out = await run_one(agent, make_row("read_file", {"path": "long.txt"}))
    assert out.status == "done" and out.result["truncated"] and len(out.result["content"]) < 2_000


async def test_rate_limit(home, tmp_path):
    cfg = make_config(home, agent__max_commands_per_minute=3)
    agent, _ = make_agent(cfg, tmp_path, monotonic=lambda: 100.0)
    outs = [await run_one(agent, make_row("list_dir")) for _ in range(4)]
    assert [o.status for o in outs] == ["done", "done", "done", "rejected"]
    assert outs[-1].result["reason"] == "rate_limited"


# loop and audit


async def test_tick_finishes_with_audit_and_no_contents(config, tmp_path):
    agent, db = make_agent(config, tmp_path, rows=[make_row("read_file", {"path": "invoice.txt"})])
    assert await agent.tick() is True
    done = db.finished[0]
    assert done["status"] == "done"
    audit = done["audit"]
    assert audit["path"] == "invoice.txt" and audit["file_bytes"] == 19
    assert "Invoice 42" not in str(audit)
    assert await agent.tick() is False


async def test_heartbeat_state(config, tmp_path):
    agent, db = make_agent(config, tmp_path)
    await db.heartbeat(agent.state())
    assert db.heartbeats[0] == {
        "paused": False,
        "read_only": True,
        "dry_run": False,
        "enabled_tools": ["delete_file", "list_dir", "move_file", "read_file", "search_files"],
    }
