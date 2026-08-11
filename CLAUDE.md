# CLAUDE.md — working on bibaudit

`bibaudit` verifies bibliographies field by field against Crossref, DataCite and
PubMed. Its users put the references it clears into manuscripts, so the bar is
not "usually right" — it is "wrong in a way you can prove, or not stated".

## The three rules that shape every decision

1. **No model in the verdict path.** Not as a fallback, not for the hard cases,
   not behind a flag. A verdict must be reproducible from the cached registry
   response by anyone, forever. If a check cannot be made deterministic, the
   tool reports the uncertainty instead of resolving it.
2. **Report, never rewrite.** The tool has no authority to decide the registry
   is right and the bibliography wrong. It never writes to a `.bib`, a `.qmd`,
   or a Zotero database. `--suggest` writes a *separate* file to diff.
3. **A false alarm costs more than a miss.** A report full of noise stops being
   read, and an unread report still looks like assurance. Every new check must
   come with its known-benign exceptions, or it does not go in.

## Layout

```
src/bibaudit/
  model.py       Reference / Record / Name / Issue / Result — the contracts
  normalize.py   clean() for display, fold() for comparison; DOI, year, page rules
  names.py       author parsing and comparison: collectives, particles, mojibake
  compare.py     the field matrix and the verdict rule
  benign.py      documented registry defects, suppressed as REGISTRY-ARTIFACT
  suppress.py    project-local .bibaudit.toml adjudications (a reason is required)
  audit.py       orchestration: adapters -> registries -> compare
  report.py      terminal and JSON output, exit codes
  suggest.py     --suggest: writes references.suggested.bib + .diff, never the original
  cli.py         argument parsing
  adapters/      bibtex, markdown (Quarto/Obsidian), zotero — read-only
  registries/    http (cache + retry), crossref, datacite,
                 pubmed (corroborates a DOI; also resolves an entry whose
                   only identifier is a PMID, by efetch alone),
                 search (Crossref + Europe PMC + OpenAlex, for entries with
                   no identifier — see "Adding a registry" below),
                 openlibrary (books: by ISBN, or by title/author search),
                 retractions (Retraction Watch's own export + PubMed's ECI
                   cross-reference, independent of Crossref's `updated-by`)

docs/
  registry-artifacts.md   every documented registry defect, with its DOI, and
                          every guard kept against one nobody has witnessed,
                          each saying which of the two it is
```

`docs/registry-artifacts.md` is not prose beside the code — it is the reader's
only way to challenge a `REGISTRY-ARTIFACT` line, and
`tests/test_benign.py::TestRuleScoping::test_every_check_is_written_up_in_the_registry_defect_docs`
turns red for any rule in `benign.CHECKS` the file does not name, and
`TestRuleScoping::test_every_author_escape_is_written_up_too` does the same for
every reason in `names.ARTIFACT_REASONS` — the author-comparison escapes, which
are decided while walking two bylines in step and so cannot be field-level
checks. What the tests enforce is that the name appears; whether the section
around it explains anything is on the person writing it. Both produce
`REGISTRY-ARTIFACT` and both owe the reader a section. Defects handled elsewhere
(a relation direction in `registries/crossref.py`) belong there too, and each
such section says where it lives.

Adapters never call registries. Registries never see a `Reference`. Comparison
never performs I/O. Keeping those boundaries is what makes the logic testable
without a network.

## Non-negotiables in code

- **404 is a fact, a timeout is ignorance.** `Transient` exists so an outage is
  never reported as a missing work. Anything that collapses the two is a bug.
- **Zotero is opened `file:...?immutable=1`.** It is the user's live library and
  their application may be writing to it.
- **DOIs contain parentheses.** `10.1016/S0140-6736(03)14065-2` is real and
  common in epidemiology. Use `normalize_doi` / `DOI_PATTERN`; never write a new
  DOI regex.
- **Never split a BibTeX author field naively on `" and "`.** `The Endogenous
  Hormones and Breast Cancer Collaborative Group` is one author.
- **Never reduce a surname to its last token.** `AragonÃ©s` folds to `aragona s`,
  whose last token is `s` — that is how a mojibake surname becomes a phantom
  mismatch.
- **Compare on `fold()`, display `clean()`.** Showing a folded value in a report
  hides the exact glyph that caused the finding.
- **First page only.** Closing pages disagree harmlessly between registries.
- **Any year the registry itself carries is acceptable**, print or online-first.
  The same goes for any container title it carries: `container-title` is an
  array and a book chapter has two, the series and the volume.
- **`updated-by` means this work was retracted; `update-to` means this work IS
  the notice.** Reading them backwards clears retracted papers and accuses the
  people who cite the notice. Publishers do deposit both directions at once —
  see `crossref._reciprocal_updates` — and the tie-break there breaks *towards*
  the finding. Never widen it without the three recorded payloads in front of
  you.
- **A retraction is the union over every registry that answered**, and a
  registry that carries the signal leaves a stated gap when it was unreachable
  *or* never asked — the second is routine, since two of the four sources take a
  DOI and an entry may carry a PMID or an ISBN instead. Ignorance about
  retraction must never render as a clean bill of health.

## Adding a check

1. Add the comparison to `compare.py`, emitting an `Issue`, never a verdict.
2. Add its known-benign exceptions to `benign.py`, each with a comment naming
   the concrete case that motivated it.
3. Write the exception up in `docs/registry-artifacts.md`. A suppression a reader
   cannot look up is one nobody can challenge, and `tests/test_benign.py` fails
   the build when a rule in `benign.CHECKS` or a reason in
   `names.ARTIFACT_REASONS` has no section.
4. Add a test in `tests/test_benign.py` proving the false positive is suppressed
   **and** a test proving the true positive still fires.
5. Extend the verdict table in `README.md` and `docs/verdicts.md` if a new
   verdict appears.

**A rule must be expressible without naming a single journal.** A journal list in
the code is the failure this rule exists to catch.

**When a review finds a defect in a rule, establish the complete set of cases
before writing anything** — not what the reported case needs. A container-title
rule that handles the leading article but not the society expansion, the
parenthetical qualifier, or that same qualifier on the abbreviation, is four
rounds of reactive fixes wearing the shape of one.

## Adding a registry

Registries are consulted for *independent* evidence. OpenAlex, Semantic Scholar
and Unpaywall largely re-crawl Crossref, so adding them adds requests without
adding corroboration. PubMed is there because it is curated separately. Apply
that test before adding anything.

Worked example, both added to `registries/search.py` in the same change and
treated oppositely: **Europe PMC is independent, OpenAlex is not.** Europe PMC
is curated separately from Crossref and indexes material Crossref does not —
preprints, grey literature, agency reports — so it corroborates a match the
way PubMed does everywhere else in this tool. OpenAlex substantially re-crawls
Crossref's own deposits, so an OpenAlex hit agreeing with a Crossref hit is one
fact read twice, not two witnesses. It failed the test, and stayed out of
corroboration entirely — but it did not get excluded outright. It is consulted
for *discovery* only, on the identifier-less path where `search.py` widens
what can be *found* by a candidate no DOI lookup can reach: every candidate,
from every source, is judged by the identical bar in
`compare.confirm_without_id` regardless of who found it, so widening discovery
never widens what gets accepted. The test decides whether a source's *answer*
counts as a second opinion, not whether the source is worth querying at all.

## Tests

```bash
uv sync --all-extras
uv run pytest                 # offline; must pass with no network
uv run pytest -m network      # opt-in, hits the real registries
uv run ruff check . && uv run mypy
```

Network tests are opt-in and deselected by default. A test suite that needs the
internet gets skipped, and a skipped suite protects nobody.

The documentation site builds with `uv run --group docs mkdocs build --strict`,
or `uv run --group docs mkdocs serve` for a live preview. `docs` is a
dependency-group rather than an extra, so `uv sync --all-extras` does not install
it.

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

### Adding a fixture

The fixtures in `tests/data/` are real registry responses, including the defective
ones. Do not "clean them up" — the mojibake and the doubled MathML are the point.

1. **Fetch the whole response and commit it unedited.** Not the fields your test
   reads — all of them. A value dropped quietly and a value invented look the same
   from outside, which is why there is no way to declare a fixture partial.
2. **Add a `[[fixture]]` entry to `tests/data/PROVENANCE.toml`** naming the file,
   the registry, the identifier you requested it under, that request's URL, and
   the date. The header of that file defines every field. A fixture with no entry
   fails the build, so this is not a step you can forget; an entry naming a URL no
   client in `src/bibaudit/registries/` would issue fails too.
3. **Run `uv run pytest -m network`.** This is the step that verifies anything.

An entry is a claim by whoever wrote it. The offline suite establishes only that
the claim hangs together — that the file really is a MEDLINE citation, that the
PMID in it is the PMID you wrote down — and a citation invented around a live PMID
satisfies every bit of that. `pytest -m network` re-fetches each URL and compares
the answer with the bytes on disk, and nothing else in this repository can tell a
recorded response from a written one. It is deselected by default so the ordinary
suite stays offline, which means a fixture nobody has re-fetched is a fixture
nobody has verified.

#### What the networked comparison ignores

MEDLINE and the Retraction Watch rows are compared with nothing ignored. Two
registries stamp their own record-keeping into the payload, and comparing on those
would redden the check every week over values no deposit carries, until nobody ran
it:

- **Crossref** — `indexed` and `deposited`, the index and last-deposit timestamps;
  `is-referenced-by-count`, a citation counter; and `link`, whose URL follows the
  publisher's hosting rather than the deposit.
- **DataCite** — `updated`, and the aggregates `viewCount`, `downloadCount`,
  `citationCount`, `referenceCount`, `partCount`, `partOfCount`, `versionCount`
  and `versionOfCount`.

Everything else is compared, and a difference means the record was revised —
re-fetch, read the diff, commit it — or the file was never that record. Widening
this list hides a fabricated value, so it grows only for a field that is
demonstrably the registry's own bookkeeping, and each addition is named here:
`tests/test_fixture_provenance.py` fails the build for one that is not.

#### If a fixture is not a registry response

A bibliography the tool *reads* — a `.bib`, a Quarto page, a Zotero export — still
needs an entry, with `source = "none"` and a `note` saying what it is. Nothing
re-fetches those, and nothing checks the identifiers inside them: their DOIs are
the thing under audit and some are wrong deliberately. The note is the only
account such a file will ever have.

Which is why one thing is demanded of the prose, and only for a file that parses
as a JSON list of objects: the note must name every `id` or `key` in it. A note
nothing reads is a note nothing corrects.

## When a change is finished

Green tests are not the bar. A change is done when:

1. **The docs are updated in the same change** — `README.md`, `CLAUDE.md`, and
   `docs/registry-artifacts.md` whenever a registry defect is involved. They are
   part of the change, not a follow-up.
2. **Every figure it states is re-derived**, not copied from a previous run, and
   says what it was measured against and when.
3. **Scratch is deleted** — repro scripts, resume notes, plan fragments. A stale
   scratch file read later as current is worse than none. Work that is genuinely
   outstanding goes in the author's project notes, not into the repository.
4. **Every behaviour added has a test that bites.** Revert each line and confirm
   something goes red. Correct code whose test stays green when the code is
   removed is untested code wearing the appearance of tested code.

## Comments state the rule, never the incident

A comment or docstring that reads like a changelog entry — "this used to return
X", "an earlier version reported Y", "without this the bug would come back" — makes
the reader reconstruct a story to learn a rule, and goes stale the moment the code
moves again. Git already records what changed and when.

The subtler form is the one that slips through: **arguing against the design you
just replaced.** "A plain dict let a caller write any string it liked" describes a
removed alternative rather than the rule that now holds. It reads as justification
to the author and as archaeology to the reader.

State the invariant in the present tense and give the reason that makes it
non-obvious — "BAD-ID requires a registry that answered; with nothing consulted the
claim is vacuous", not "this used to report BAD-ID and that was wrong". Naming a
concrete motivating *case* is different and is required in `benign.py`; what to
avoid is narrating the fix. This holds for the docs too, where "an earlier version
did X" is worse still.

## Git

- No attribution trailers in commit messages, and no mention of the assistant
  that helped write a change. A commit message is about the change, not about
  who typed it.
- Commit messages state what changed and why, in the imperative. The history a
  comment must not carry belongs here.
- Keep the diff to one concern. A change to the verdict path is worth reviewing
  on its own.

The rule above is about commit messages only. The README and the documentation
site *do* acknowledge that this was built with Claude Code, deliberately and at
the author's instruction — the point being that a model wrote the comparison
rules while no model evaluates one. Do not remove those sections as if they
were stray attribution.
