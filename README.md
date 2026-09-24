# safeedit

Bulk regex edits across a tree of files, done the careful way. Standard library only, Python 3.9+.

It is the tool form of a real job: adding `aria-label` to about 2,600 PHP pages on a live site.
It does the boring safety work that a one-off `sed -i` skips.

```
python -m safeedit edit /var/www/site --glob '*.php' \
  --find '(<a\b[^<>]*?\bclass="share-btn facebook")([^<>]*>)' \
  --replace '\1 aria-label="Share on Facebook"\2' \
  --skip-if 'aria-label' --dotall \
  --loose '<a\b[^>]*class="share-btn facebook"' \
  --prove-additive ' aria-label="Share on Facebook"' \
  --lint 'php -l {}'
```

That is a **dry run**: nothing is written. It reports how many files would change, shows example
diffs, and lists every place the loose search found that the strict pattern did not handle. To
write, add `--apply BACKUP.tar.gz`. To undo: `python -m safeedit restore BACKUP.tar.gz --root /var/www/site`.

## What it does that `sed -i` does not

| Guard | What it means |
| --- | --- |
| Dry run by default | You see the count and examples first. Writing needs `--apply`, which needs a backup path. |
| Backup before any write | Originals go in a `.tar.gz`, stored relative to `--root`. `restore` refuses archive paths that escape the root. |
| Additive proof | `--prove-additive REGEX` describes the text you are inserting. Remove it from the old and the new file: they must be identical, or that file is not written. This proves the edit changed nothing else. `--apply` needs it unless you pass `--no-proof`. |
| Per-file lint | `--lint 'php -l {}'` runs on each changed file. A non-zero exit puts the original back. |
| Unhandled-match report | `--loose` is a wider search. Anything it finds that `--find` did not cover is listed with file and line, so the pattern's blind spots are visible instead of silent. `--fail-on-unhandled` turns them into exit code 1 (and refuses `--apply`). |
| `--skip-if` | Leaves alone matches that are already fixed, so a re-run changes nothing (idempotent). |
| Recent-file guard | Files modified in the last 300 s are skipped (`--skip-recent`), because a generator may be writing them. |
| File mode kept | Permissions are preserved. |

## The modification-time trap

By default a changed file gets a **new** modification time. `--keep-mtime` keeps the old one, which
looks tidier but has a cost: PHP's OPcache checks files by mtime, so an edit that keeps the mtime is
invisible until the server is reloaded. On the live site above, the edit was applied and the pages
looked unchanged until a graceful `systemctl reload apache2`. Use `--keep-mtime` only when you will
reload the cache yourself.

## Exit codes

`0` ok, `1` some file failed the proof or lint (or unhandled matches with `--fail-on-unhandled`),
`2` bad arguments (an invalid regex, a missing root or archive).

## Limits

- Text files only; files are read as UTF-8 with undecodable bytes carried through unchanged.
- The additive proof shows the edit *only inserted the described text*. It cannot say the insertion is correct; that is what the dry-run examples and `--lint` are for.
- Regexes are Python `re`. HTML/PHP with `<` inside attributes or PHP blocks inside a tag defeats simple patterns; that is exactly what `--loose` reports.

## Tests

```
python -m unittest discover -s tests -v
```

MIT licence.
