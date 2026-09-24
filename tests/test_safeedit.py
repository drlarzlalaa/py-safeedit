import contextlib
import io
import os
import re
import sys
import tarfile
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import safeedit as se
from safeedit.__main__ import main

TAG = r'(<a\b[^<>]*?\bclass="share-btn facebook")([^<>]*>)'
FIND = re.compile(TAG, re.S)
REPLACE = r'\1 aria-label="Share on Facebook"\2'


def rule(**kw):
    base = dict(find=FIND, replace=REPLACE, skip_if=re.compile("aria-label"), loose=re.compile(r'<a\b[^>]*class="share-btn facebook"', re.S),
                prove=re.compile(r' aria-label="Share on Facebook"'))
    base.update(kw)
    return se.Rule(**base)


PLAIN = '<p>hi</p>\n<a class="share-btn facebook"\n   href="https://x/y"\n   target="_blank"><i></i></a>\n<a class="share-btn twitter" href="t">t</a>\n'
DONE = '<a class="share-btn facebook" aria-label="Share on Facebook" href="https://x/y"><i></i></a>\n'
PHP = '<a class="share-btn facebook" href="https://x/?u=<?php echo urlencode($u); ?>" target="_blank"><i></i></a>\n'
ODD = '<a data-x="1<2" class="share-btn facebook" href="x">odd</a>\n'


class Tree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.old = time.time() - 86400
        for name, text in {"a.php": PLAIN, "done.php": DONE, "php.php": PHP, "none.php": "<p>nothing</p>\n", "sub/b.php": PLAIN, ".git/x.php": PLAIN,
                           "skip.txt": PLAIN}.items():
            self.write(name, text)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text, age=86400):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.utime(path, (time.time() - age, time.time() - age))
        return path

    def read(self, name):
        with open(os.path.join(self.root, name), encoding="utf-8") as fh:
            return fh.read()

    def path(self, name):
        return os.path.join(self.root, name)


class Files(Tree):
    def test_glob_and_excludes(self):
        found = sorted(os.path.relpath(p, self.root) for p in se.iter_files(self.root, ["*.php"]))
        self.assertEqual(found, ["a.php", "done.php", "none.php", "php.php", os.path.join("sub", "b.php")])      # no .git, no .txt

    def test_several_globs(self):
        found = [os.path.basename(p) for p in se.iter_files(self.root, ["*.txt", "none.php"])]
        self.assertEqual(sorted(found), ["none.php", "skip.txt"])


class EditText(unittest.TestCase):
    def test_multi_line_tag(self):
        new, made, skipped = se.edit_text(PLAIN, rule())
        self.assertEqual((made, skipped), (1, 0))
        self.assertIn('class="share-btn facebook" aria-label="Share on Facebook"\n   href', new)
        self.assertIn('class="share-btn twitter" href="t"', new)                       # other platforms untouched

    def test_skip_if(self):
        new, made, skipped = se.edit_text(DONE, rule())
        self.assertEqual((new, made, skipped), (DONE, 0, 1))

    def test_no_match(self):
        self.assertEqual(se.edit_text("<p>x</p>", rule()), ("<p>x</p>", 0, 0))

    def test_group_references(self):
        r = se.Rule(find=re.compile(r"(\d+)-(\d+)"), replace=r"\2-\1")
        self.assertEqual(se.edit_text("10-20 and 3-4", r)[0], "20-10 and 4-3")


class Planning(Tree):
    def test_dry_run_writes_nothing(self):
        before = {n: self.read(n) for n in ("a.php", "sub/b.php")}
        p = se.plan(self.root, ["*.php"], rule())
        self.assertEqual({n: self.read(n) for n in before}, before)
        self.assertEqual(sorted(os.path.relpath(c.path, self.root) for c in p.changes), ["a.php", os.path.join("sub", "b.php")])
        self.assertEqual((p.replacements, p.unchanged_matches, p.files_scanned), (2, 1, 5))

    def test_unhandled_matches_are_found_with_line_numbers(self):
        self.write("odd.php", "line one\n" + ODD)
        p = se.plan(self.root, ["*.php"], rule())
        self.assertEqual([(os.path.basename(a), b) for a, b, _ in p.unhandled], [("odd.php", 2), ("php.php", 1)])

    def test_php_inside_a_tag_needs_a_pattern_that_allows_it(self):
        strict = se.plan(self.root, ["php.php"], rule())
        self.assertEqual((len(strict.changes), len(strict.unhandled)), (0, 1))            # the strict pattern cannot parse it, and says so
        php_aware = re.compile(r'(<a\b(?:[^<>]|<\?.*?\?>)*?\bclass="share-btn facebook")((?:[^<>]|<\?.*?\?>)*>)', re.S)
        ok = se.plan(self.root, ["php.php"], rule(find=php_aware))
        self.assertEqual((len(ok.changes), len(ok.unhandled)), (1, 0))

    def test_recently_touched_files_are_skipped(self):
        self.write("fresh.php", PLAIN, age=5)
        p = se.plan(self.root, ["fresh.php"], rule(), skip_recent=300)
        self.assertEqual((len(p.changes), [os.path.basename(x) for x in p.recent]), (0, ["fresh.php"]))
        p = se.plan(self.root, ["fresh.php"], rule(), skip_recent=0)
        self.assertEqual(len(p.changes), 1)


class Additive(Tree):
    def test_proof_holds_for_an_insertion(self):
        p = se.plan(self.root, ["a.php"], rule())
        self.assertTrue(se.is_additive(p.changes[0], rule()))

    def test_proof_fails_when_the_edit_changes_more(self):
        bad = rule(replace=r'\1 aria-label="Share on Facebook" data-changed="1"\2', prove=re.compile(r' aria-label="Share on Facebook"'))
        p = se.plan(self.root, ["a.php"], bad)
        self.assertFalse(se.is_additive(p.changes[0], bad))

    def test_without_a_proof_regex_nothing_is_claimed(self):
        r = rule(prove=None)
        self.assertTrue(se.is_additive(se.plan(self.root, ["a.php"], r).changes[0], r))


class Applying(Tree):
    def do_apply(self, **kw):
        p = se.plan(self.root, ["*.php"], rule())
        return se.apply(p, rule(), self.root, os.path.join(self.root, "..", "bk-%s.tar.gz" % os.path.basename(self.root)), **kw)

    def test_apply_edits_and_backs_up(self):
        res = self.do_apply()
        self.assertEqual((len(res.applied), res.failed), (2, []))
        self.assertIn('aria-label="Share on Facebook"', self.read("a.php"))
        with tarfile.open(res.backup) as tar:
            self.assertEqual(sorted(tar.getnames()), ["a.php", os.path.join("sub", "b.php")])
            self.assertEqual(tar.extractfile("a.php").read().decode(), PLAIN)
        os.remove(res.backup)

    def test_apply_is_idempotent(self):
        self.do_apply()
        p = se.plan(self.root, ["*.php"], rule())
        self.assertEqual((len(p.changes), p.replacements), (0, 0))

    def test_a_failing_lint_restores_the_file(self):
        p = se.plan(self.root, ["*.php"], rule())
        lint = "case {} in */sub/*) exit 1;; esac"           # fails only for the file under sub/
        res = se.apply(p, rule(), self.root, os.path.join(self.root, "..", "bk2-%s.tar.gz" % os.path.basename(self.root)), lint=lint)
        self.assertEqual(len(res.applied), 1)
        self.assertEqual(len(res.failed), 1)
        self.assertIn("lint failed (restored)", res.failed[0][1])
        self.assertEqual(self.read("sub/b.php"), PLAIN)                                          # restored to the original
        self.assertIn("aria-label", self.read("a.php"))
        os.remove(res.backup)

    def test_mtime_is_updated_by_default_and_kept_on_request(self):
        before = os.stat(self.path("a.php")).st_mtime
        self.do_apply()
        self.assertGreater(os.stat(self.path("a.php")).st_mtime, before + 1000)             # updated: caches keyed on mtime notice it
        self.write("c.php", PLAIN)
        old = os.stat(self.path("c.php")).st_mtime
        p = se.plan(self.root, ["c.php"], rule())
        se.apply(p, rule(), self.root, os.path.join(self.root, "..", "bk3-%s.tar.gz" % os.path.basename(self.root)), keep_mtime=True)
        self.assertAlmostEqual(os.stat(self.path("c.php")).st_mtime, old, delta=1)

    def test_file_mode_is_preserved(self):
        os.chmod(self.path("a.php"), 0o640)
        self.do_apply()
        self.assertEqual(os.stat(self.path("a.php")).st_mode & 0o777, 0o640)

    def test_unprovable_edits_are_not_written(self):
        bad = rule(replace=r'\1 aria-label="Share on Facebook" data-changed="1"\2')
        p = se.plan(self.root, ["a.php"], bad)
        res = se.apply(p, bad, self.root, os.path.join(self.root, "..", "bk4-%s.tar.gz" % os.path.basename(self.root)))
        self.assertEqual((res.applied, len(res.failed)), ([], 1))
        self.assertEqual(self.read("a.php"), PLAIN)


class Restore(Tree):
    def test_round_trip(self):
        p = se.plan(self.root, ["*.php"], rule())
        bk = os.path.join(self.root, "..", "rt-%s.tar.gz" % os.path.basename(self.root))
        se.apply(p, rule(), self.root, bk)
        self.assertNotEqual(self.read("a.php"), PLAIN)
        done = se.restore(bk, self.root)
        self.assertEqual(len(done), 2)
        self.assertEqual(self.read("a.php"), PLAIN)
        os.remove(bk)

    def test_refuses_paths_that_escape_the_root(self):
        evil = os.path.join(self.root, "..", "evil-%s.tar.gz" % os.path.basename(self.root))
        with tarfile.open(evil, "w:gz") as tar:
            info = tarfile.TarInfo("../outside.txt")
            data = b"x"
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        with self.assertRaises(ValueError):
            se.restore(evil, self.root)
        os.remove(evil)


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class Cli(Tree):
    args = ["edit", None, "--glob", "*.php", "--find", TAG, "--replace", REPLACE, "--skip-if", "aria-label", "--dotall",
            "--loose", r'<a\b[^>]*class="share-btn facebook"', "--prove-additive", r' aria-label="Share on Facebook"']

    def a(self, *extra):
        a = list(self.args)
        a[1] = self.root
        return a + list(extra)

    def test_dry_run(self):
        code, out, _ = run_cli(*self.a())
        self.assertEqual(code, 0)
        self.assertIn("5 files scanned; 2 would change (2 replacements); 1 matches skipped by --skip-if", out)
        self.assertIn("additive proof: holds for every changed file", out)
        self.assertIn("nothing was written", out)
        self.assertIn('+ <a class="share-btn facebook" aria-label="Share on Facebook"', out)
        self.assertEqual(self.read("a.php"), PLAIN)

    def test_apply_requires_a_proof_or_an_explicit_waiver(self):
        a = self.a("--apply", os.path.join(self.root, "..", "cli-bk.tar.gz"))
        no_proof = [x for x in a if x != r' aria-label="Share on Facebook"']
        no_proof.remove("--prove-additive")
        code, _, err = run_cli(*no_proof)
        self.assertEqual(code, 2)
        self.assertIn("--prove-additive", err)

    def test_apply_then_restore(self):
        bk = os.path.join(self.root, "..", "cli-%s.tar.gz" % os.path.basename(self.root))
        code, out, _ = run_cli(*self.a("--apply", bk, "--lint", "test -s {}"))
        self.assertEqual(code, 0)
        self.assertIn("applied to 2 files; 0 left unchanged or restored", out)
        self.assertIn("aria-label", self.read("a.php"))
        code, out, _ = run_cli(*self.a())
        self.assertIn("0 would change", out)                                                # idempotent
        code, out, _ = run_cli("restore", bk, "--root", self.root)
        self.assertEqual(code, 0)
        self.assertEqual(self.read("a.php"), PLAIN)
        os.remove(bk)

    def test_unhandled_matches_can_block_an_apply(self):
        self.write("odd.php", ODD)
        code, out, _ = run_cli(*self.a("--fail-on-unhandled"))
        self.assertEqual(code, 1)
        self.assertIn("unhandled by the strict pattern: 2", out)
        code, _, err = run_cli(*self.a("--fail-on-unhandled", "--apply", os.path.join(self.root, "..", "x.tar.gz")))
        self.assertEqual(code, 1)
        self.assertIn("refusing to apply", err)

    def test_bad_regex_and_missing_root(self):
        code, _, err = run_cli("edit", self.root, "--glob", "*.php", "--find", "(unclosed", "--replace", "x")
        self.assertEqual(code, 2)
        self.assertIn("not a valid regular expression", err)
        code, _, err = run_cli("restore", "/no/such.tar.gz", "--root", self.root)
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
