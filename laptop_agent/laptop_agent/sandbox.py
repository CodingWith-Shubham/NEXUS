"""The path sandbox: every path is resolved and checked here before any file is touched.

Rules, in order, for every path a command names:

1. Paths are relative to ``~/Downloads`` ("Invoices/a.pdf", "Downloads/Invoices/a.pdf"
   and "~/Downloads/Invoices/a.pdf" all mean the same file). Absolute paths are
   accepted only if they land inside the allowed roots.
2. The path is resolved with ``realpath`` BEFORE any check, then must be inside
   an allowed root. This catches ``..``, symlinks and aliases pointing elsewhere.
3. Any symlink anywhere in the path is refused, even one pointing inside Downloads.
4. Nothing inside the trash folder can be read, listed or moved.
5. No path component may be hidden (start with ".") or match a deny pattern.
"""

import fnmatch
import os
import stat
from pathlib import Path

from laptop_agent.config import AgentConfig


class SandboxError(Exception):
    """A path or operation the agent refuses. ``code`` is short and safe to show."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


class Sandbox:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.downloads = config.downloads
        self.home = config.downloads.parent
        self.roots = config.allowed_roots
        self.trash = config.trash_dir
        self._patterns = tuple(p.lower() for p in config.deny_patterns)

    # names

    def is_denied_name(self, name: str) -> bool:
        lowered = name.lower()
        return any(fnmatch.fnmatchcase(lowered, p) for p in self._patterns)

    def is_hidden_name(self, name: str) -> bool:
        return name.startswith(".")

    def rel(self, path: Path) -> str:
        """How a path is shown: always relative to the home folder, e.g. "Downloads/a.pdf"."""
        if path == self.downloads:
            return "Downloads"
        return "Downloads/" + path.relative_to(self.downloads).as_posix()

    # resolution

    def _candidate(self, raw: str) -> Path:
        if not isinstance(raw, str):
            raise SandboxError("invalid", "path must be text")
        raw = raw.strip()
        if "\x00" in raw:
            raise SandboxError("invalid", "path contains a null byte")
        if raw.startswith("~"):
            if raw != "~" and not raw.startswith("~/"):
                raise SandboxError("outside_scope", "only ~/Downloads is accessible")
            joined = str(self.home) + raw[1:]
        elif os.path.isabs(raw):
            joined = raw
        else:
            if raw == "Downloads" or raw.startswith("Downloads/"):
                raw = raw[len("Downloads") :].lstrip("/")
            joined = os.path.join(self.downloads, raw)
        return Path(os.path.normpath(joined))

    def resolve(self, raw: str) -> Path:
        """Resolve a user path to a real path inside the sandbox, or raise SandboxError."""
        candidate = self._candidate(raw)
        real = Path(os.path.realpath(candidate))
        if not any(_is_within(real, root) for root in self.roots):
            raise SandboxError("outside_scope", "only the Downloads folder is accessible")
        if candidate != real:
            raise SandboxError("symlink", "paths through symlinks or aliases are not allowed")
        if _is_within(real, self.trash):
            raise SandboxError("trash", "the JARVIS trash folder is not accessible")
        if real != self.downloads:
            for part in real.relative_to(self.downloads).parts:
                if self.is_hidden_name(part):
                    raise SandboxError("denied", "hidden files and folders are not accessible")
                if self.is_denied_name(part):
                    raise SandboxError("denied", "that file is on the protected list")
        return real

    # file type checks (lstat: never follows a symlink)

    def require_regular_file(self, path: Path) -> os.stat_result:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            raise SandboxError("not_found", "no such file") from None
        if stat.S_ISLNK(st.st_mode):
            raise SandboxError("symlink", "symlinks are not allowed")
        if not stat.S_ISREG(st.st_mode):
            raise SandboxError("not_a_file", "only regular files are allowed")
        return st

    def require_dir(self, path: Path) -> os.stat_result:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            raise SandboxError("not_found", "no such folder") from None
        if stat.S_ISLNK(st.st_mode):
            raise SandboxError("symlink", "symlinks are not allowed")
        if not stat.S_ISDIR(st.st_mode):
            raise SandboxError("not_a_dir", "that is not a folder")
        return st
