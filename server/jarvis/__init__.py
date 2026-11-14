"""JARVIS core: storage, tool registry, skill loader, approval gate and task queue.

Nothing in this package knows about any particular skill pack. Packs live in
``skills/<name>/`` and are loaded by name from ``jarvis.toml``.
"""
