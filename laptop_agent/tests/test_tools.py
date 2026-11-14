import os

import pytest
from conftest import make_config

from laptop_agent.sandbox import Sandbox, SandboxError
from laptop_agent.tools import FileTools


def tools_for(config):
    return FileTools(config, Sandbox(config))


@pytest.fixture
def tools(config):
    return tools_for(config)


def _code(fn, *args):
    with pytest.raises(SandboxError) as e:
        fn(*args)
    return e.value.code


# list / search / read


def test_list_dir_shows_files_and_hides_denied_hidden_and_symlinks(tools, home, downloads):
    (downloads / "api_token.txt").write_text("x")
    (downloads / ".DS_Store").write_text("x")
    os.symlink(home / "Documents", downloads / "docs")
    (downloads / ".jarvis_trash").mkdir()
    listing = tools.list_dir("")
    names = [e["name"] for e in listing["entries"]]
    assert names == [".jarvis_trash", "invoice.txt", "Invoices", "notes.md"]
    assert listing["path"] == "Downloads"
    file = next(e for e in listing["entries"] if e["name"] == "invoice.txt")
    assert file["type"] == "file" and file["size"] == len("Invoice 42: 100 USD")
    assert tools.list_dir("Invoices")["entries"][0]["name"] == "march.csv"


def test_list_dir_refuses_outside_and_trash(tools, downloads):
    (downloads / ".jarvis_trash").mkdir()
    assert _code(tools.list_dir, "~/Documents") == "outside_scope"
    assert _code(tools.list_dir, ".jarvis_trash") == "trash"
    assert _code(tools.list_dir, "invoice.txt") == "not_a_dir"


def test_search_is_capped_and_skips_denied_hidden_symlinks_and_trash(home, downloads):
    for i in range(10):
        (downloads / f"report-{i}.txt").write_text("x")
    (downloads / "report_password.txt").write_text("x")
    (downloads / ".report-hidden.txt").write_text("x")
    (downloads / ".jarvis_trash").mkdir()
    (downloads / ".jarvis_trash" / "report-old.txt").write_text("x")
    (home / "Documents" / "report-doc.txt").write_text("x")
    os.symlink(home / "Documents", downloads / "linked")
    os.symlink(downloads / "report-1.txt", downloads / "report-link.txt")
    tools = tools_for(make_config(home, paths__max_search_results=5))
    found = tools.search_files("REPORT")
    paths = [r["path"] for r in found["results"]]
    assert len(paths) == 5 and found["truncated"]
    all_found = tools_for(make_config(home, paths__max_search_results=50)).search_files("report")
    paths = {r["path"] for r in all_found["results"]}
    assert paths == {f"Downloads/report-{i}.txt" for i in range(10)}
    assert _code(tools.search_files, "") == "invalid"
    assert _code(tools.search_files, "../x") == "invalid"


def test_read_file_text_only_and_capped(home, downloads, tools):
    assert tools.read_file("invoice.txt")["content"] == "Invoice 42: 100 USD"
    (downloads / "photo.jpg").write_bytes(b"\xff\xd8")
    assert _code(tools.read_file, "photo.jpg") == "unsupported_type"
    (downloads / "fake.txt").write_bytes(b"abc\x00def")
    assert _code(tools.read_file, "fake.txt") == "unsupported_type"
    (downloads / "big.txt").write_text("x" * 300)
    small = tools_for(make_config(home, paths__max_read_bytes=100))
    assert _code(small.read_file, "big.txt") == "too_large"
    chars = tools_for(make_config(home, paths__max_read_chars=50)).read_file("big.txt")
    assert len(chars["content"]) == 50 and chars["truncated"]


def test_read_refuses_outside_ssh_denied_and_trash(tools, downloads):
    assert _code(tools.read_file, "~/.ssh/id_rsa") == "outside_scope"
    assert _code(tools.read_file, "~/Documents/private.txt") == "outside_scope"
    (downloads / "prod.env").write_text("KEY=1")
    assert _code(tools.read_file, "prod.env") == "denied"
    (downloads / ".jarvis_trash").mkdir()
    (downloads / ".jarvis_trash" / "old.txt").write_text("x")
    assert _code(tools.read_file, ".jarvis_trash/old.txt") == "trash"


# move


def test_move_into_folder_and_rename(tools, downloads):
    result = tools.move_file("invoice.txt", "Invoices")
    assert result["moved"]["to"] == "Downloads/Invoices/invoice.txt"
    assert (downloads / "Invoices" / "invoice.txt").exists()
    assert not (downloads / "invoice.txt").exists()
    tools.move_file("Invoices/invoice.txt", "Invoices/inv-42.txt")
    assert (downloads / "Invoices" / "inv-42.txt").read_text() == "Invoice 42: 100 USD"


def test_move_never_overwrites(tools, downloads):
    (downloads / "Invoices" / "invoice.txt").write_text("existing")
    assert _code(tools.move_file, "invoice.txt", "Invoices") == "exists"
    assert _code(tools.move_file, "invoice.txt", "notes.md") == "exists"
    assert (downloads / "Invoices" / "invoice.txt").read_text() == "existing"
    assert (downloads / "notes.md").read_text().startswith("# Notes")
    assert (downloads / "invoice.txt").exists()


def test_move_dest_outside_is_refused(tools, home, downloads):
    assert _code(tools.move_file, "invoice.txt", "~/Documents") == "outside_scope"
    assert _code(tools.move_file, "invoice.txt", "../Desktop/invoice.txt") == "outside_scope"
    os.symlink(home / "Documents", downloads / "docs")
    assert _code(tools.move_file, "invoice.txt", "docs") == "outside_scope"
    assert _code(tools.move_file, "invoice.txt", ".jarvis_trash/x.txt") == "trash"
    assert _code(tools.move_file, "invoice.txt", "secret.txt") == "denied"
    assert _code(tools.move_file, "invoice.txt", "NoSuchFolder/x.txt") == "not_found"
    assert (downloads / "invoice.txt").exists()
    assert os.listdir(home / "Documents") == ["private.txt"]


def test_move_and_delete_refuse_directories_and_symlinks(tools, downloads):
    os.symlink(downloads / "invoice.txt", downloads / "link.txt")
    assert _code(tools.move_file, "Invoices", "Moved") == "not_a_file"
    assert _code(tools.move_file, "link.txt", "Invoices") == "symlink"
    assert _code(tools.delete_file, "Invoices") == "not_a_file"
    assert _code(tools.delete_file, "link.txt") == "symlink"
    assert (downloads / "Invoices").is_dir() and os.path.islink(downloads / "link.txt")


# delete


def test_delete_lands_in_jarvis_trash(tools, downloads):
    result = tools.delete_file("invoice.txt")
    assert result["trashed"] == "Downloads/invoice.txt"
    assert not (downloads / "invoice.txt").exists()
    trashed = os.listdir(downloads / ".jarvis_trash")
    assert len(trashed) == 1 and trashed[0].endswith("_invoice.txt")
    assert (downloads / ".jarvis_trash" / trashed[0]).read_text() == "Invoice 42: 100 USD"
    (downloads / "invoice.txt").write_text("again")
    tools.delete_file("invoice.txt")
    assert len(os.listdir(downloads / ".jarvis_trash")) == 2
    assert _code(tools.list_dir, ".jarvis_trash") == "trash"
    assert _code(tools.delete_file, ".jarvis_trash/" + trashed[0]) == "trash"


def test_delete_refuses_trash_that_is_a_symlink(tools, home, downloads):
    os.symlink(home / "Documents", downloads / ".jarvis_trash")
    with pytest.raises(SandboxError):
        tools.delete_file("invoice.txt")
    assert (downloads / "invoice.txt").exists()
    assert os.listdir(home / "Documents") == ["private.txt"]


def test_dry_run_changes_nothing(home, downloads):
    tools = tools_for(make_config(home, agent__dry_run=True))
    before = sorted(os.listdir(downloads))
    moved = tools.move_file("invoice.txt", "Invoices")
    trashed = tools.delete_file("notes.md")
    assert moved["dry_run"] and moved["would_move"]["to"] == "Downloads/Invoices/invoice.txt"
    assert trashed["dry_run"] and trashed["would_trash"] == "Downloads/notes.md"
    assert sorted(os.listdir(downloads)) == before
    assert not (downloads / ".jarvis_trash").exists()
