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
  registries/    http (cache + retry), crossref, datacite, pubmed,
                 search (Crossref + Europe PMC + OpenAlex, for entries with
                   no identifier — see "Adding a registry" below),
                 openlibrary (books: by ISBN, or by title/author search),
                 retractions (Retraction Watch's own export + PubMed's ECI
                   cross-reference, independent of Crossref's `updated-by`)

docs/
  registry-artifacts.md   every documented registry defect, with its DOI
```

`docs/registry-artifacts.md` is not prose beside the code — it is the reader's
only way to challenge a `REGISTRY-ARTIFACT` line, and
`tests/test_benign.py::test_every_check_is_written_up_in_the_registry_defect_docs`
turns red for any rule in `benign.CHECKS` that has no section there. Defects
handled outside `benign.py` (a list shape in `names.py`, a relation direction in
`registries/crossref.py`) belong there too, and each such section says where it
lives.

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
- **A retraction is the union over every registry that answered**, and an
  unreachable registry that carries the signal leaves a stated gap. Ignorance
  about retraction must never render as a clean bill of health.

## Adding a check

1. Add the comparison to `compare.py`, emitting an `Issue`, never a verdict.
2. Add its known-benign exceptions to `benign.py`, each with a comment naming
   the concrete case that motivated it.
3. Add a test in `tests/test_benign.py` proving the false positive is suppressed
   **and** a test proving the true positive still fires.
4. Extend the verdict table in `README.md` if a new verdict appears.

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
uv run pytest                 # offline; must pass with no network
uv run pytest -m network      # opt-in, hits the real registries
uv run ruff check . && uv run mypy
```

Network tests are opt-in and deselected by default. A test suite that needs the
internet gets skipped, and a skipped suite protects nobody.

The fixtures in `tests/data/` are real registry responses, including the
defective ones. Do not "clean them up" — the mojibake and the doubled MathML are
the point.

## Git

- No attribution trailers in commit messages, and no mention of the assistant
  that helped write a change. A commit message is about the change, not about
  who typed it.
- Commit messages state what changed and why, in the imperative.

The rule above is about commit messages only. The README and the documentation
site *do* acknowledge that this was built with Claude Code, deliberately and at
the author's instruction — the point being that a model wrote the comparison
rules while no model evaluates one. Do not remove those sections as if they
were stray attribution.
