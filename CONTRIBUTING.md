# Contributing

The rules this project is held to are in [`CLAUDE.md`](CLAUDE.md). They are
short, and they are the reason the tool behaves the way it does — read them
before changing comparison logic.

## Getting set up

```bash
uv sync --all-extras
uv run pytest                 # offline; must pass with no network
uv run ruff check . && uv run mypy
```

`uv run pytest -m network` is opt-in and hits the real registries. It is
deselected by default because a test suite that needs the internet gets
skipped, and a skipped suite protects nobody.

The documentation site builds with `uv run --group docs mkdocs build --strict`,
or `uv run --group docs mkdocs serve` for a live preview. The `docs` group is
a dependency-group, not an extra, so `uv sync --all-extras` above does not
install it.

### Testing a change in a throwaway copy

**Do not `cp -R` the repo.** The copied `.venv` holds an editable install pointing
at the *original* absolute path, so `import bibaudit` inside the copy loads the
original source: the edit appears to do nothing, the tests "pass", and any
conclusion drawn from them is worthless. Build the tree from git instead:

```bash
rm -rf /tmp/iso && mkdir -p /tmp/iso
git archive HEAD | tar -x -C /tmp/iso
cd /tmp/iso && uv sync --all-extras -q
uv run python -c "import bibaudit.compare as m; print(m.__file__)"   # must be /tmp/iso
```

In a copy, run `uv run python -m pytest` rather than `uv run pytest` — the latter
executes the venv's console script, whose shebang points back at the original.
Checks that read files by *path* are unaffected; anything that imports the package
is not, and the tell is a modification that changes no behaviour at all.

## When a change is finished

Green tests are not the bar. A change is done when:

1. **The docs are updated in the same change** — `README.md`, `CLAUDE.md`, and
   `docs/registry-artifacts.md` whenever a registry defect is involved. They are
   part of the change, not a follow-up.
2. **Every figure it states is re-derived**, not copied from a previous run, and
   says what it was measured against and when.
3. **Scratch is deleted** — repro scripts, resume notes, plan fragments. Work that
   is genuinely outstanding goes in `TASKS.md`, which stays; a stale scratch file
   read later as current is worse than none.
4. **Every behaviour added has a test that bites.** Revert each line and confirm
   something goes red. This project has repeatedly shipped correct code whose test
   stayed green when the code was removed.

## The three rules, in short

1. **No model in the verdict path.** A verdict must be reproducible from the
   cached registry response by anyone, indefinitely. If a check cannot be made
   deterministic, the tool reports the uncertainty instead of resolving it.
2. **Report, never rewrite.** Nothing is ever written to a `.bib`, a `.qmd` or a
   Zotero database. `--suggest` writes a *separate* file to diff.
3. **A false alarm costs more than a miss.** A report full of noise stops being
   read, and an unread report still looks like assurance.

## Adding a check

1. Add the comparison to `compare.py`, emitting an `Issue`, never a verdict.
2. Add its known-benign exceptions to `benign.py`, each with a comment naming
   the concrete case that motivated it.
3. Write the exception up in `docs/registry-artifacts.md`. This is not optional
   politeness — a suppression a reader cannot look up is one nobody can
   challenge, and `tests/test_benign.py` fails the build when a rule in
   `benign.CHECKS` or a reason in `names.ARTIFACT_REASONS` has no section.
4. Add a test proving the false positive is suppressed **and** one proving the
   true positive still fires.
5. Extend the verdict table in `README.md` and `docs/verdicts.md` if a new
   verdict appears.

## Adding a fixture

Fixtures in `tests/data/` are real registry responses, including the defective
ones. Do not "clean them up" — the mojibake and the doubled MathML are the
point.

That rule was written down here and in `CLAUDE.md` and enforced by nothing, and
ten files broke it. Every one was found by somebody who had opened it for an
unrelated reason. So:

1. **Fetch the whole response and commit it unedited.** Not the fields your
   test reads — all of them. A value dropped quietly and a value invented look
   the same from outside, which is why there is no way to declare a fixture
   partial.
2. **Add a `[[fixture]]` entry to `tests/data/PROVENANCE.toml`** naming the
   file, the registry, the identifier you requested it under, that request's
   URL, and the date. The header of that file defines every field. A fixture
   with no entry fails the build, so this is not a step you can forget; an
   entry naming a URL no client in `src/bibaudit/registries/` would issue
   fails too.
3. **Run `uv run pytest -m network`.** This is the step that verifies
   anything.

An entry is a claim by whoever wrote it. The offline suite checks only that the
claim hangs together — that the file really is a MEDLINE citation, that the
PMID in it is the PMID you wrote down — and a citation invented around a live
PMID satisfies every bit of that. Four of the ten were exactly that.
`pytest -m network` re-fetches each URL and compares the answer with the bytes
on disk, and it is the only thing here that can tell a recorded response from a
written one. It is deselected by default so the ordinary suite stays offline,
which means a fixture nobody has re-fetched is a fixture nobody has verified.

### What the networked comparison ignores

MEDLINE and the Retraction Watch rows are compared with nothing ignored. Two
registries stamp their own record-keeping into the payload, and comparing on
those would redden the check every week over values no deposit carries, until
nobody ran it:

- **Crossref** — `indexed` and `deposited`, the index and last-deposit
  timestamps; `is-referenced-by-count`, a citation counter; and `link`, whose
  URL follows the publisher's hosting (one moved from `journals.lww.com` to
  `www.ovid.com` inside nine days with every bibliographic field unchanged).
- **DataCite** — `updated`, and the aggregates `viewCount`, `downloadCount`,
  `citationCount`, `referenceCount`, `partCount`, `partOfCount`,
  `versionCount` and `versionOfCount`.

Everything else is compared, and a difference means the record was revised —
re-fetch, read the diff, commit it — or the file was never that record.
Widening this list hides a fabricated value, so it grows only for a field that
is demonstrably the registry's own bookkeeping, and each addition is named
here: `tests/test_fixture_provenance.py` fails the build for one that is not.

### If a fixture is not a registry response

A bibliography the tool *reads* — a `.bib`, a Quarto page, a Zotero export —
still needs an entry, with `source = "none"` and a `note` saying what it is.
Nothing re-fetches those, and nothing checks the identifiers inside them:
their DOIs are the thing under audit and some are wrong deliberately. The note
is the only account such a file will ever have.

Which is why one thing is demanded of the prose, and only for a file that
parses as a JSON list of objects: the note must name every `id` or `key` in
it. Both Zotero notes described "two items" over a file of four, from the
commit that introduced the manifest to the one that noticed, because a note
nothing reads is a note nothing corrects.

## Adding a registry

Registries are consulted for *independent* evidence. OpenAlex, Semantic Scholar
and Unpaywall largely re-crawl Crossref, so adding them adds requests without
adding corroboration. PubMed is there because it is curated separately. Apply
that test before adding anything — and note it decides whether a source's
*answer* counts as a second opinion, not whether the source is worth querying
for discovery at all.

## Pull requests

- Commit messages state what changed and why, in the imperative. No attribution
  trailers.
- Keep the diff to one concern. A change to the verdict path is worth reviewing
  on its own.
- A new or changed file in `tests/data/` needs the steps under
  [Adding a fixture](#adding-a-fixture), the third of which is not automated.

## Reporting a wrong verdict

Open an issue with the entry as stored, the DOI or ISBN, and what the tool said.
Because every verdict is derived from a cached registry response, the cache file
is usually enough to settle it: `bibaudit cache info` will tell you where it
lives.
