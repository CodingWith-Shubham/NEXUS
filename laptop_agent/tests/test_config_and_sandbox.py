import os
from pathlib import Path

import pytest
from conftest import make_config

from laptop_agent.config import HARD_DENY_PATTERNS, ConfigError
from laptop_agent.limits import RateLimiter, cap_output
from laptop_agent.sandbox import Sandbox, SandboxError

# config: can only narrow


def test_defaults_are_restrictive(config, downloads):
    assert config.read_only is True
    assert config.dry_run is False
    assert config.allowed_roots == (Path(os.path.realpath(downloads)),)
    assert set(HARD_DENY_PATTERNS) <= set(config.deny_patterns)


def test_allowed_roots_cannot_be_widened(home):
    for root in ["~/Documents", "~", "/", "~/Downloads/../Documents", str(home / ".ssh")]:
        with pytest.raises(ConfigError):
            make_config(home, paths__allowed_roots=[root])


def test_allowed_roots_can_be_narrowed(home, downloads):
    cfg = make_config(home, paths__allowed_roots=["~/Downloads/Invoices"])
    sb = Sandbox(cfg)
    assert sb.resolve("Invoices/march.csv").name == "march.csv"
    with pytest.raises(SandboxError) as e:
        sb.resolve("invoice.txt")
    assert e.value.code == "outside_scope"


def test_deny_list_cannot_be_shortened_and_tools_must_be_known(home):
    cfg = make_config(home, deny__patterns=["*.docx"])
    assert "*.pem" in cfg.deny_patterns and "*.docx" in cfg.deny_patterns
    with pytest.raises(ConfigError):
        make_config(home, tools__enabled=["list_dir", "run_script"])


def test_trash_dir_is_fixed(home):
    with pytest.raises(ConfigError):
        make_config(home, paths__trash_dir="~/Documents/.jarvis_trash")


# sandbox


def _code(fn, *args):
    with pytest.raises(SandboxError) as e:
        fn(*args)
    return e.value.code


def test_relative_and_prefixed_paths_resolve_inside(sandbox, downloads):
    real = Path(os.path.realpath(downloads / "invoice.txt"))
    assert sandbox.resolve("invoice.txt") == real
    assert sandbox.resolve("Downloads/invoice.txt") == real
    assert sandbox.resolve("~/Downloads/invoice.txt") == real
    assert sandbox.resolve("") == Path(os.path.realpath(downloads))
    assert sandbox.rel(real) == "Downloads/invoice.txt"


def test_dotdot_escape_is_rejected(sandbox):
    assert _code(sandbox.resolve, "../Documents/private.txt") == "outside_scope"
    assert _code(sandbox.resolve, "Invoices/../../Documents") == "outside_scope"
    assert _code(sandbox.resolve, "..") == "outside_scope"


def test_absolute_path_outside_is_rejected(sandbox, home):
    assert _code(sandbox.resolve, str(home / "Documents" / "private.txt")) == "outside_scope"
    assert _code(sandbox.resolve, "/etc/passwd") == "outside_scope"
    assert _code(sandbox.resolve, "~/Desktop") == "outside_scope"
    assert _code(sandbox.resolve, "~root/x") == "outside_scope"


def test_ssh_is_rejected(sandbox):
    assert _code(sandbox.resolve, "~/.ssh/id_rsa") == "outside_scope"
    assert _code(sandbox.resolve, "../.ssh/id_rsa") == "outside_scope"


def test_symlink_to_documents_is_rejected(sandbox, home, downloads):
    os.symlink(home / "Documents", downloads / "docs")
    os.symlink(home / "Documents" / "private.txt", downloads / "private.txt")
    assert _code(sandbox.resolve, "docs/private.txt") == "outside_scope"
    assert _code(sandbox.resolve, "private.txt") == "outside_scope"


def test_symlink_inside_downloads_is_also_rejected(sandbox, downloads):
    os.symlink(downloads / "invoice.txt", downloads / "alias.txt")
    assert _code(sandbox.resolve, "alias.txt") == "symlink"


def test_hard_deny_and_hidden_names(sandbox, downloads):
    for name in ["server.PEM", "my_Secret_plan.txt", "API_TOKEN.txt", "id_rsa.pub", "x.kdbx",
                 "Wallet.json", "seed phrase.txt", "cert.p12", ".env", "prod.env", "a.keychain-db"]:  # fmt: skip
        (downloads / name).write_text("x")
        assert _code(sandbox.resolve, name) == "denied", name
    (downloads / "secrets").mkdir()
    (downloads / "secrets" / "plain.txt").write_text("x")
    assert _code(sandbox.resolve, "secrets/plain.txt") == "denied"
    (downloads / ".hidden.txt").write_text("x")
    assert _code(sandbox.resolve, ".hidden.txt") == "denied"


def test_trash_is_not_accessible(sandbox, downloads):
    (downloads / ".jarvis_trash").mkdir()
    (downloads / ".jarvis_trash" / "old.txt").write_text("x")
    assert _code(sandbox.resolve, ".jarvis_trash") == "trash"
    assert _code(sandbox.resolve, ".jarvis_trash/old.txt") == "trash"


def test_file_type_checks(sandbox, downloads):
    assert _code(sandbox.require_regular_file, sandbox.resolve("Invoices")) == "not_a_file"
    os.symlink(downloads / "invoice.txt", downloads / "link.txt")
    assert _code(sandbox.require_regular_file, downloads / "link.txt") == "symlink"
    assert _code(sandbox.require_regular_file, downloads / "missing.txt") == "not_found"
    os.mkfifo(downloads / "pipe")
    assert _code(sandbox.require_regular_file, sandbox.resolve("pipe")) == "not_a_file"


# limits


def test_rate_limiter_rolls_over():
    now = [0.0]
    rl = RateLimiter(3, clock=lambda: now[0])
    assert [rl.allow() for _ in range(4)] == [True, True, True, False]
    now[0] = 60.0
    assert rl.allow()


def test_cap_output_truncates_content_and_lists():
    big = cap_output({"content": "x" * 10_000}, 1000)
    assert big["truncated"] and len(big["content"]) < 1000
    lst = cap_output({"results": [{"name": "f" * 50}] * 500}, 2000)
    assert lst["truncated"] and len(lst["results"]) < 500
    small = {"content": "ok"}
    assert cap_output(small, 1000) is small
