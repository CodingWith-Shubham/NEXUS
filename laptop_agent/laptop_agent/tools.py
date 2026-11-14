"""The agent's own tool table and the file operations behind it.

Standard library only. Nothing is ever opened in an app or executed; files are
only listed, read as bytes, hard-linked and unlinked. Every path goes through
:class:`~laptop_agent.sandbox.Sandbox` first.
"""

import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from laptop_agent.config import TRASH_NAME, AgentConfig
from laptop_agent.sandbox import Sandbox, SandboxError

TEXT_EXTENSIONS = frozenset({".txt", ".md", ".csv", ".json", ".log"})
MAX_ARG_CHARS = 1024
MAX_QUERY_CHARS = 100


@dataclass(frozen=True)
class ToolDef:
    name: str
    tier: int  # 0: read-only, no approval. 2: changes files, needs approval + read_only off.
    required: frozenset[str]
    optional: frozenset[str] = frozenset()


# The ONLY tools the agent will run. The tier comes from here, never from the command row.
TOOL_TABLE: dict[str, ToolDef] = {
    t.name: t
    for t in [
        ToolDef("list_dir", 0, frozenset(), frozenset({"path"})),
        ToolDef("search_files", 0, frozenset({"query"})),
        ToolDef("read_file", 0, frozenset({"path"})),
        ToolDef("move_file", 2, frozenset({"src", "dest"})),
        ToolDef("delete_file", 2, frozenset({"path"})),
        ToolDef("pause", 0, frozenset()),
    ]
}


def valid_args(tool: ToolDef, args) -> bool:
    if not isinstance(args, dict):
        return False
    keys = set(args)
    if not tool.required <= keys or not keys <= tool.required | tool.optional:
        return False
    return all(isinstance(v, str) and len(v) <= MAX_ARG_CHARS for v in args.values())


def _modified(st: os.stat_result) -> str:
    return datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="minutes")


class FileTools:
    def __init__(self, config: AgentConfig, sandbox: Sandbox):
        self.config = config
        self.sb = sandbox

    # Tier 0

    def list_dir(self, path: str = "") -> dict:
        folder = self.sb.resolve(path)
        self.sb.require_dir(folder)
        entries: list[dict] = []
        truncated = False
        scanned = 0
        with os.scandir(folder) as it:
            items = sorted(it, key=lambda e: e.name.lower())
        for entry in items:
            scanned += 1
            if scanned > self.config.max_scan_entries:
                truncated = True
                break
            if entry.is_symlink():
                continue
            if entry.name == TRASH_NAME and folder == self.sb.downloads:
                entries.append({"name": TRASH_NAME, "type": "folder", "note": "JARVIS trash"})
                continue
            if self.sb.is_hidden_name(entry.name) or self.sb.is_denied_name(entry.name):
                continue
            st = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                item = {"name": entry.name, "type": "folder", "modified": _modified(st)}
            elif stat.S_ISREG(st.st_mode):
                item = {
                    "name": entry.name,
                    "type": "file",
                    "size": st.st_size,
                    "modified": _modified(st),
                }
            else:
                continue
            if len(entries) >= self.config.max_list_entries:
                truncated = True
                break
            entries.append(item)
        return {"path": self.sb.rel(folder), "entries": entries, "truncated": truncated}

    def search_files(self, query: str) -> dict:
        q = query.strip().lower()
        if not q or len(q) > MAX_QUERY_CHARS or "/" in q or "\x00" in q:
            raise SandboxError("invalid", "search needs a short part of a file name")
        results: list[dict] = []
        seen: set[Path] = set()
        scanned = 0
        truncated = False
        for root in self.config.allowed_roots:
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                here = Path(dirpath)
                dirnames[:] = sorted(
                    name
                    for name in dirnames
                    if not self.sb.is_hidden_name(name)
                    and not self.sb.is_denied_name(name)
                    and not os.path.islink(here / name)
                    and here / name != self.sb.trash
                )
                for name in sorted(filenames):
                    scanned += 1
                    if scanned > self.config.max_scan_entries:
                        truncated = True
                        break
                    if q not in name.lower():
                        continue
                    if self.sb.is_hidden_name(name) or self.sb.is_denied_name(name):
                        continue
                    full = here / name
                    if full in seen:
                        continue
                    try:
                        st = os.lstat(full)
                        if not stat.S_ISREG(st.st_mode):
                            continue
                        self.sb.resolve(self.sb.rel(full))  # defense in depth
                    except (OSError, SandboxError):
                        continue
                    seen.add(full)
                    results.append(
                        {"path": self.sb.rel(full), "size": st.st_size, "modified": _modified(st)}
                    )
                    if len(results) >= self.config.max_search_results:
                        truncated = True
                        break
                if truncated:
                    break
            if truncated:
                break
        return {"query": query.strip(), "results": results, "truncated": truncated}

    def read_file(self, path: str) -> dict:
        file = self.sb.resolve(path)
        st = self.sb.require_regular_file(file)
        if file.suffix.lower() not in TEXT_EXTENSIONS:
            raise SandboxError("unsupported_type", "only txt, md, csv, json and log files can be read")
        if st.st_size > self.config.max_read_bytes:
            raise SandboxError("too_large", "that file is too large to read")
        fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            opened = os.fstat(f.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_ino != st.st_ino:
                raise SandboxError("not_a_file", "the file changed while being read")
            data = f.read(self.config.max_read_bytes + 1)
        if len(data) > self.config.max_read_bytes:
            raise SandboxError("too_large", "that file is too large to read")
        if b"\x00" in data:
            raise SandboxError("unsupported_type", "that looks like a binary file")
        text = data.decode("utf-8", errors="replace")
        truncated = len(text) > self.config.max_read_chars
        return {
            "path": self.sb.rel(file),
            "size": st.st_size,
            "content": text[: self.config.max_read_chars],
            "truncated": truncated,
        }

    # Tier 2

    def move_file(self, src: str, dest: str) -> dict:
        source = self.sb.resolve(src)
        st = self.sb.require_regular_file(source)
        target = self.sb.resolve(dest)
        try:
            is_dir = stat.S_ISDIR(os.lstat(target).st_mode)
        except FileNotFoundError:
            is_dir = False
        if is_dir:
            target = self.sb.resolve(self.sb.rel(target / source.name))
        else:
            self.sb.require_dir(target.parent)
        if target == source:
            raise SandboxError("invalid", "source and destination are the same")
        if os.path.lexists(target):
            raise SandboxError("exists", "a file with that name already exists there")
        moved = {"from": self.sb.rel(source), "to": self.sb.rel(target), "size": st.st_size}
        if self.config.dry_run:
            return {"dry_run": True, "would_move": moved}
        try:
            os.link(source, target)  # fails if the target exists: never overwrites
        except FileExistsError:
            raise SandboxError("exists", "a file with that name already exists there") from None
        os.unlink(source)
        return {"moved": moved}

    def delete_file(self, path: str) -> dict:
        source = self.sb.resolve(path)
        st = self.sb.require_regular_file(source)
        if self.config.dry_run:
            return {"dry_run": True, "would_trash": self.sb.rel(source), "size": st.st_size}
        trash = self.sb.trash
        if os.path.lexists(trash):
            self.sb.require_dir(trash)
        else:
            os.mkdir(trash, 0o700)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"{stamp}_{source.name}"
        n = 1
        while os.path.lexists(trash / name):
            name = f"{stamp}-{n}_{source.name}"
            n += 1
        os.link(source, trash / name)
        os.unlink(source)
        return {"trashed": self.sb.rel(source), "size": st.st_size}
