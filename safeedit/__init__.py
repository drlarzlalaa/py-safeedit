"""safeedit - make the same regex edit across many files, with the safety net a bulk edit needs.

Dry run by default. When applying, it backs the files up first, can PROVE the edit is purely additive,
runs a lint command on every changed file and restores any file that fails, skips files that were
touched moments ago, and reports the places a looser search found that your strict pattern did not
handle. Standard library only.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import tarfile
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

__version__ = "0.1.0"

DEFAULT_EXCLUDES = (".git", "node_modules", "vendor", "__pycache__")


@dataclass
class Rule:
    """One edit: replace every match of ``find`` with ``replace`` unless the matched text also matches ``skip_if``."""

    find: "re.Pattern"
    replace: str
    skip_if: Optional["re.Pattern"] = None
    loose: Optional["re.Pattern"] = None            # a wider search: matches it finds that ``find`` does not cover are reported
    prove: Optional["re.Pattern"] = None            # the inserted text; removing it from old and new must give identical text


@dataclass
class FileChange:
    path: str
    old: str
    new: str
    replaced: int
    skipped: int


@dataclass
class Plan:
    changes: List[FileChange] = field(default_factory=list)
    unchanged_matches: int = 0                      # matches skipped by skip_if
    unhandled: List[Tuple[str, int, str]] = field(default_factory=list)   # (path, line, text) found by the loose search only
    recent: List[str] = field(default_factory=list)
    files_scanned: int = 0

    @property
    def replacements(self) -> int:
        return sum(c.replaced for c in self.changes)


def iter_files(root: str, patterns: Iterable[str], excludes: Iterable[str] = DEFAULT_EXCLUDES) -> Iterable[str]:
    excludes = set(excludes)
    patterns = list(patterns)
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in excludes)
        for f in sorted(files):
            if any(fnmatch.fnmatch(f, p) for p in patterns):
                yield os.path.join(d, f)


def _read(path: str) -> Tuple[bytes, str]:
    with open(path, "rb") as fh:
        raw = fh.read()
    return raw, raw.decode("utf-8", "surrogateescape")


def edit_text(text: str, rule: Rule) -> Tuple[str, int, int]:
    """Apply ``rule`` to ``text``. Returns (new text, replacements made, matches skipped)."""
    made = skipped = 0

    def sub(m):
        nonlocal made, skipped
        if rule.skip_if is not None and rule.skip_if.search(m.group(0)):
            skipped += 1
            return m.group(0)
        made += 1
        return m.expand(rule.replace)

    return rule.find.sub(sub, text), made, skipped


def plan(root: str, patterns: Iterable[str], rule: Rule, excludes: Iterable[str] = DEFAULT_EXCLUDES, skip_recent: float = 300.0,
         now: Optional[float] = None) -> Plan:
    """Work out every change without writing anything."""
    p = Plan()
    now = time.time() if now is None else now
    for path in iter_files(root, patterns, excludes):
        raw, text = _read(path)
        p.files_scanned += 1
        if rule.loose is not None:
            for m in rule.loose.finditer(text):
                if rule.find.match(text, m.start()) is None:
                    line = text.count("\n", 0, m.start()) + 1
                    p.unhandled.append((path, line, text[m.start(): m.start() + 100].replace("\n", " ")))
        new, made, skipped = edit_text(text, rule)
        p.unchanged_matches += skipped
        if new == text:
            continue
        if now - os.stat(path).st_mtime < skip_recent:
            p.recent.append(path)
            continue
        p.changes.append(FileChange(path, text, new, made, skipped))
    return p


def is_additive(change: FileChange, rule: Rule) -> bool:
    """True if removing the text described by ``rule.prove`` from both versions leaves identical text."""
    if rule.prove is None:
        return True
    return rule.prove.sub("", change.new) == rule.prove.sub("", change.old)


@dataclass
class ApplyResult:
    applied: List[str] = field(default_factory=list)
    failed: List[Tuple[str, str]] = field(default_factory=list)      # (path, reason)
    backup: str = ""


def backup_files(paths: List[str], backup: str, root: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(backup)) or ".", exist_ok=True)
    with tarfile.open(backup, "w:gz") as tar:
        for path in paths:
            tar.add(path, arcname=os.path.relpath(os.path.abspath(path), os.path.abspath(root)))


def apply(p: Plan, rule: Rule, root: str, backup: str, lint: Optional[str] = None, keep_mtime: bool = False,
          runner: Callable = subprocess.run) -> ApplyResult:
    """Write the planned changes. Backs up first; restores any file that fails the additive proof or the lint."""
    res = ApplyResult(backup=backup)
    backup_files([c.path for c in p.changes], backup, root)
    for c in p.changes:
        if not is_additive(c, rule):
            res.failed.append((c.path, "the edit is not purely additive; file left unchanged"))
            continue
        st = os.stat(c.path)
        original = c.old.encode("utf-8", "surrogateescape")
        with open(c.path, "wb") as fh:
            fh.write(c.new.encode("utf-8", "surrogateescape"))
        os.chmod(c.path, st.st_mode)
        if lint:
            cmd = lint.replace("{}", _quote(c.path))
            done = runner(cmd, shell=True, capture_output=True, text=True)
            if done.returncode != 0:
                with open(c.path, "wb") as fh:
                    fh.write(original)
                os.utime(c.path, (st.st_atime, st.st_mtime))
                res.failed.append((c.path, "lint failed (restored): " + (done.stdout + done.stderr).strip()[:150]))
                continue
        if keep_mtime:
            os.utime(c.path, (st.st_atime, st.st_mtime))
        res.applied.append(c.path)
    return res


def _quote(path: str) -> str:
    return "'" + path.replace("'", "'\\''") + "'"


def restore(backup: str, root: str) -> List[str]:
    """Put every file in a backup archive back under ``root``."""
    restored = []
    with tarfile.open(backup, "r:gz") as tar:
        for member in tar.getmembers():
            target = os.path.abspath(os.path.join(root, member.name))
            if not target.startswith(os.path.abspath(root) + os.sep):
                raise ValueError("refusing to restore outside the root: %s" % member.name)
            member.name = os.path.relpath(target, os.path.abspath(root))
            tar.extract(member, os.path.abspath(root))
            restored.append(target)
    return restored
