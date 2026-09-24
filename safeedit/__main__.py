"""Command line: python -m safeedit edit|restore ..."""
from __future__ import annotations

import argparse
import difflib
import re
import sys

from . import DEFAULT_EXCLUDES, Rule, __version__, apply, is_additive, plan, restore


def _compile(text: str, flags: int, what: str):
    try:
        return re.compile(text, flags)
    except re.error as exc:
        print("error: --%s is not a valid regular expression: %s" % (what, exc), file=sys.stderr)
        sys.exit(2)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="safeedit", description="Make one regex edit across many files, safely.")
    parser.add_argument("--version", action="version", version="safeedit " + __version__)
    sub = parser.add_subparsers(dest="command", required=True)
    e = sub.add_parser("edit", help="plan (and optionally apply) a regex replacement across files")
    e.add_argument("root", help="directory to search")
    e.add_argument("--glob", action="append", required=True, metavar="PATTERN", help="file name pattern, e.g. '*.php' (repeatable)")
    e.add_argument("--find", required=True, help="regular expression to find")
    e.add_argument("--replace", required=True, help=r"replacement, with \1 or \g<name> for groups")
    e.add_argument("--skip-if", help="leave a match alone if the matched text also matches this expression (e.g. it is already fixed)")
    e.add_argument("--loose", help="a wider search: matches it finds that --find does not cover are listed as unhandled")
    e.add_argument("--prove-additive", metavar="REGEX", help="describes the inserted text; the edit must give identical files once it is removed from old and new")
    e.add_argument("--no-proof", action="store_true", help="allow --apply without --prove-additive")
    e.add_argument("--dotall", action="store_true", help="let . match newlines")
    e.add_argument("--multiline", action="store_true", help="let ^ and $ match at line boundaries")
    e.add_argument("--exclude", action="append", default=list(DEFAULT_EXCLUDES), help="directory name to skip (repeatable; default %s)" % ", ".join(DEFAULT_EXCLUDES))
    e.add_argument("--skip-recent", type=float, default=300.0, metavar="SECONDS", help="skip files modified this recently (default 300)")
    e.add_argument("--sample", type=int, default=2, help="how many changed files to show as examples in a dry run")
    e.add_argument("--apply", metavar="BACKUP.tar.gz", help="write the changes, after saving the originals to this archive")
    e.add_argument("--lint", metavar="COMMAND", help="command run on each changed file ({} is the path); a non-zero exit restores the file")
    e.add_argument("--keep-mtime", action="store_true", help="give changed files their old modification time (see the README: caches keyed on mtime will not notice)")
    e.add_argument("--fail-on-unhandled", action="store_true", help="exit 1 if the loose search found anything the strict pattern did not handle")
    r = sub.add_parser("restore", help="put files back from a backup made by edit --apply")
    r.add_argument("backup")
    r.add_argument("--root", required=True, help="the same root directory that was given to edit")
    args = parser.parse_args(argv)

    if args.command == "restore":
        try:
            done = restore(args.backup, args.root)
        except (OSError, ValueError) as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 2
        print("restored %d files under %s" % (len(done), args.root))
        return 0

    flags = (re.S if args.dotall else 0) | (re.M if args.multiline else 0)
    rule = Rule(find=_compile(args.find, flags, "find"), replace=args.replace,
                skip_if=_compile(args.skip_if, flags, "skip-if") if args.skip_if else None,
                loose=_compile(args.loose, flags, "loose") if args.loose else None,
                prove=_compile(args.prove_additive, flags, "prove-additive") if args.prove_additive else None)
    if args.apply and rule.prove is None and not args.no_proof:
        print("error: --apply needs --prove-additive REGEX (or --no-proof if the edit is not purely additive)", file=sys.stderr)
        return 2
    try:
        p = plan(args.root, args.glob, rule, args.exclude, args.skip_recent)
    except OSError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    print("%d files scanned; %d would change (%d replacements); %d matches skipped by --skip-if; %d files skipped as recently modified" % (
        p.files_scanned, len(p.changes), p.replacements, p.unchanged_matches, len(p.recent)))
    if rule.loose is not None:
        print("unhandled by the strict pattern: %d" % len(p.unhandled))
        for path, line, text in p.unhandled[:5]:
            print("  %s:%d  %s" % (path, line, text))
    unprovable = [c.path for c in p.changes if not is_additive(c, rule)]
    if rule.prove is not None:
        print("additive proof: %s" % ("holds for every changed file" if not unprovable else "FAILS for %d file(s), e.g. %s" % (len(unprovable), unprovable[0])))
    if not args.apply:
        for c in p.changes[: args.sample]:
            print("\n--- example: %s" % c.path)
            shown = 0
            for a, b in zip(c.old.splitlines(), c.new.splitlines()):
                if a != b and shown < 3:
                    print("  - " + a.strip()[:140])
                    print("  + " + b.strip()[:160])
                    shown += 1
        print("\n(dry run: nothing was written. Add --apply BACKUP.tar.gz to write the changes.)")
        return 1 if unprovable or (args.fail_on_unhandled and p.unhandled) else 0
    if args.fail_on_unhandled and p.unhandled:
        print("error: refusing to apply while --fail-on-unhandled finds unhandled matches", file=sys.stderr)
        return 1
    res = apply(p, rule, args.root, args.apply, args.lint, args.keep_mtime)
    print("backed up %d files to %s" % (len(p.changes), res.backup))
    print("applied to %d files; %d left unchanged or restored" % (len(res.applied), len(res.failed)))
    for path, why in res.failed[:10]:
        print("  %s: %s" % (path, why))
    return 1 if res.failed else 0


if __name__ == "__main__":
    sys.exit(main())
