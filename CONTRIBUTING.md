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
or `mkdocs serve` for a live preview.

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
- Fixtures in `tests/data/` are real registry responses, including the defective
  ones. Do not "clean them up" — the mojibake and the doubled MathML are the
  point.

## Reporting a wrong verdict

Open an issue with the entry as stored, the DOI or ISBN, and what the tool said.
Because every verdict is derived from a cached registry response, the cache file
is usually enough to settle it: `bibaudit cache info` will tell you where it
lives.
