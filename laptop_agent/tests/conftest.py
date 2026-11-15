"""A fake home folder for every test: the real ~/Downloads is never touched."""

import os

import pytest

from laptop_agent.config import config_from_dict
from laptop_agent.sandbox import Sandbox


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    downloads = home / "Downloads"
    downloads.mkdir(parents=True)
    (downloads / "invoice.txt").write_text("Invoice 42: 100 USD")
    (downloads / "notes.md").write_text("# Notes\nhello")
    (downloads / "Invoices").mkdir()
    (downloads / "Invoices" / "march.csv").write_text("a,b\n1,2\n")
    (home / "Documents").mkdir()
    (home / "Documents" / "private.txt").write_text("private")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_rsa").write_text("KEY")
    (home / "Desktop").mkdir()
    return home


@pytest.fixture
def downloads(home):
    return home / "Downloads"


def make_config(home, **overrides):
    data: dict = {"agent": {}, "paths": {}, "deny": {}, "tools": {}}
    for key, value in overrides.items():
        section, name = key.split("__")
        data[section][name] = value
    return config_from_dict(data, home=home)


@pytest.fixture
def config(home):
    return make_config(home)


@pytest.fixture
def sandbox(config):
    return Sandbox(config)


def symlink(target, link):
    os.symlink(target, link)
