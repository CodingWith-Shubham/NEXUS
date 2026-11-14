"""The agent package must never run programs, evaluate code or open listening sockets."""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "laptop_agent"

FORBIDDEN_MODULES = {
    "subprocess", "pty", "webbrowser", "ctypes", "multiprocessing", "socket",
    "socketserver", "http.server", "xmlrpc.server", "code", "codeop", "runpy",
    "urllib", "urllib.request", "requests", "httpx", "shutil", "pickle", "marshal",
}  # fmt: skip
FORBIDDEN_CALL_NAMES = {"eval", "exec", "compile", "__import__", "breakpoint"}
FORBIDDEN_ATTRS = {
    "system", "popen", "startfile", "fork", "forkpty", "posix_spawn", "posix_spawnp",
    "execv", "execve", "execl", "execle", "execlp", "execlpe", "execvp", "execvpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "create_subprocess_exec", "create_subprocess_shell", "start_server", "create_server",
    "start_unix_server", "rmtree", "remove", "rmdir", "removedirs", "chmod", "chown",
}  # fmt: skip


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in FORBIDDEN_MODULES or alias.name.split(".")[0] in FORBIDDEN_MODULES:
                    found.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod in FORBIDDEN_MODULES or mod.split(".")[0] in FORBIDDEN_MODULES:
                found.append(f"from {mod} import")
            for alias in node.names:
                if alias.name in FORBIDDEN_ATTRS | FORBIDDEN_CALL_NAMES:
                    found.append(f"from {mod} import {alias.name}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_CALL_NAMES:
            found.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            found.append(f".{node.attr}")
        elif isinstance(node, ast.keyword) and node.arg == "shell":
            found.append("shell=")
    return [f"{path.name}: {v}" for v in found]


def test_package_files_found():
    assert len(list(PACKAGE.glob("*.py"))) >= 6


def test_no_subprocess_system_eval_exec_or_servers():
    violations = [v for f in sorted(PACKAGE.glob("*.py")) for v in _violations(f)]
    assert violations == []


def test_the_scanner_catches_things(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import subprocess\nimport os\nos.system('x')\neval('1')\nexec('x')\n"
        "subprocess.run('x', shell=True)\n"
    )
    v = _violations(bad)
    assert {"bad.py: import subprocess", "bad.py: .system", "bad.py: eval", "bad.py: exec",
            "bad.py: shell="} <= set(v)  # fmt: skip
