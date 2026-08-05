# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

A change to what the tool *reports* is a user-visible change even when no
signature moved, so verdict and wording changes are listed here alongside API
ones. That is the point of the file: somebody deciding whether to re-run an
audit needs to know whether the answer could differ.

## [Unreleased]

Not yet published to PyPI. Install with
`uv tool install git+https://github.com/lorenzoFabbri/bibaudit`.

The documentation toolchain is a PEP 735 dependency-group rather than an extra,
so `uv sync --all-extras` does not install it into the test environment.

### Added

- **A PMID is read as an identifier in its own right, and resolved.** BibTeX's
  `pmid` field, an `eprint` paired with `eprinttype = {pubmed}` (or the older
  `archiveprefix`), CSL's own `PMID` variable, and a `PMID: 28520842` label
  opening a line of a Zotero `Extra` box or a CSL `note` all reach
  `Reference.pmid`. An entry carrying a PMID and no DOI is resolved through
  PubMed's `efetch` alone — one request where a DOI costs three — instead of
  being searched for by title and author, and its title, authors, year,
  journal and the rest are compared against the MEDLINE citation exactly as a
  DOI-resolved entry's are. A label mid-sentence is not a declaration: MEDLINE
  back-matter pasted into an `Extra` box (`Comment in: JAMA. 2003;289:2560.
  PMID: 12759325`) names a correction, not the work being cited, and is not
  read.
- **What `efetch` answers with decides the verdict, and there are three
  answers.** A citation under the number asked for resolves the entry. A
  response carrying no citation under it is `BAD-ID` — narrower evidence than
  the wording suggests, because `efetch` omits what it has nothing for rather
  than saying anything: the same empty response comes back for a number NLM
  never assigned and for one it assigned and later withdrew. A response
  carrying a citation under some *other* number, or a request that failed
  outright, is `UNCHECKED` with an `identifier/inconclusive` finding naming
  what came back instead — an answer about another record is not an answer
  about this identifier, and only an absence may accuse a bibliography. PubMed
  unreachable, or `--no-corroborate` leaving no registry to ask at all, is
  `UNCHECKED` too.
- **A PMID stored beside a DOI is compared, not looked up.** The DOI fetches
  the record, so the PMID is a second, independent claim about which work is
  cited; PubMed answering for that DOI under a different number means the two
  identifiers name two citations, reported as `pmid/mismatch` and added to
  `compare.CHECKED_FIELDS`. It is a **warning**: the verdict is `INCOMPLETE`
  and the exit code 0, because only one side of the comparison was looked up —
  nothing asks PubMed what the *stored* number names, and a number that has
  since stopped answering is invisible from this side. It takes `--verbose` to
  see and `--fail-on INCOMPLETE` to bite. Two ways it declines to fire:
  `Record.pmid` is left unset when PubMed answered for the DOI under more than
  one PMID, so an entry storing either is never accused; and a stored number
  that is the record's own PubMed Central accession — NLM issues both for one
  article and Zotero keeps them on adjacent `Extra` lines — is a
  `REGISTRY-ARTIFACT`.
- **A source that was never asked is stated, not passed over.** A reference
  reaching fewer than all four retraction sources now carries a
  `status/not-asked` finding naming them, and the run prints it beside the
  banner as it prints an outage, on its own line ending `not asked`.
  `consulted` names `crossref`, `datacite`, `pubmed` and `retraction-watch` on
  every reference in every run, so an unasked source reads as `not-asked`
  rather than as a key that is not there. This closes the gap a PMID opens: a
  reference resolved by its PMID keeps NLM's `PT - Retracted Publication`,
  which is a field of the MEDLINE record the lookup already returned, and is
  asked nothing else — Retraction Watch's export, Crossref's `updated-by` and
  PubMed's own `ECI` cross-reference all take a DOI it does not carry, so an
  expression of concern about such an entry goes unreported. A book resolved by
  its ISBN and a run given `--no-retraction-check` reach the same finding by
  the same rule.
- `pmid` joins the `field` values a `.bibaudit.toml` `[[ignore]]` rule can name,
  alongside `doi`, `isbn`, `identifier` and `status`.
- Documentation site at <https://lorenzofabbri.github.io/bibaudit/>, built with
  MkDocs Material and gated by `mkdocs build --strict`.
- `py.typed` marker (PEP 561), so the package's annotations are visible to type
  checkers in projects that depend on it, and a `Typing :: Typed` classifier.
- `RetractionStatus`, returned by `Retractions.status_for`, carrying the
  retraction notices and the names of sources that could not be reached, and
  `RetractionOutage`, a `Transient` naming every retraction source a raised
  outage took down rather than only the leg that raised.
- Per-version `Programming Language :: Python :: 3.11/3.12/3.13` classifiers.
- `names.Reason`, an enum of every explanation the author comparison can attach
  to a position, and `names.ARTIFACT_REASONS` derived from it, so the set of
  author-comparison escapes that produce a `REGISTRY-ARTIFACT` suppression
  cannot fall behind the reasons that exist. Each member also states whether an
  agreement ending in it counts as a creator that aligned, which decides what
  the omitted-first-author and interleaved-consortia rules may treat as
  evidence. `AuthorDiff.reasons` is a read-only mapping written only through
  `AuthorDiff.note` and `AuthorDiff.note_collectives`, which take a `Reason`;
  together with `names_agree`'s narrowed return type this makes `mypy` reject a
  reason that is not in the enum, where the previous source-scanning test could
  not see one at all. A test now fails the build when
  `docs/registry-artifacts.md` does not name one of them — the contract
  `benign.CHECKS` already had. Five sections were written to satisfy it:
  particle filing, compound surnames shortened to their final element,
  one-character spelling variants, author lists in a different order, and the
  four comparisons that agree without being evidence of anything.
- `CONTRIBUTING.md`, `SECURITY.md`, a Dependabot configuration, a coverage floor
  (`fail_under = 92`, with branch coverage on), and a tag-triggered release
  workflow using PyPI Trusted Publishing.

### Changed

- **An entry carrying a PMID is no longer searched for by title and author.**
  It is resolved by that PMID instead, which is the exact answer a similarity
  search was standing in for — so such an entry no longer receives a proposed
  DOI from a search candidate under `--suggest`. Nothing is ever proposed for a
  missing PMID either: gap-filling works from a `missing` finding, and no check
  raises one for an identifier the entry never claimed.
- The retraction banner prints one line per reason rather than one line for all
  of them, so an outage and a source nobody asked are counted and named
  separately. A rerun may settle the first; no rerun asks a DOI-keyed source
  about a reference with no DOI.
- **`--no-isbn` on a book stored with only an ISBN now reports `UNCHECKED`
  rather than `BAD-ID`, and no longer fails the run.** The flag switches off the
  only registry organised around books, and "resolves in no consulted registry"
  is vacuously true when nothing was consulted. A malformed ISBN still fails,
  and its issue `kind` changed from `unresolved` to `malformed` — visible in
  `--format json` output and in `.bibaudit.toml` rules that match on `kind`.
- `UNCHECKED`'s description now covers both of its causes: no registry answered,
  or none was asked.
- `bibtexparser` gained an upper bound (`<3`). Naming a pre-release makes
  resolvers accept every later pre-release, and a lockfile constrains this
  repository rather than an install of the published package.

### Fixed

- **The journal titles MEDLINE files a serial under no longer fail a correct
  entry.** NLM drops a leading article and appends a place qualifier, so `JT`
  is `Lancet (London, England)` where a bibliography stores `The Lancet`. On a
  PMID-resolved entry PubMed is the only registry consulted and that was the
  only container value there was, so every correct Lancet, BMJ or Science entry
  was a `FIELD-MISMATCH` and the run exited 1. MEDLINE's `TA` is now carried as
  an alternate title as well (`Lancet` gets an `info` note naming PubMed as the
  registry that holds it), and a stored value differing by one opening
  `The`/`A`/`An` is a `REGISTRY-ARTIFACT`. The qualifier is never stripped:
  `(London, England)` is what tells two serials sharing a base title apart.
- **An `et al.` written as a Zotero creator no longer counts as an author.**
  Zotero's single-field creator — `fieldMode` 1 in the database, `name` in its
  item JSON, `literal` in CSL — carries both a corporate byline and the
  truncation marker a producer that stores only the first author writes for the
  rest. Counted as a name it made every such entry disagree with the registry
  on author count, which `compare` hands to the verdict as evidence the
  identifier resolved to a different work. It is now read as truncation on all
  three routes, which voids the length comparison and nothing else — a wrong
  first author beside the marker still fires.
- A Retraction Watch outage is now reported instead of passed over. The bulk
  export failing was absorbed and returned as a bare `dict`, so nothing reached
  the run's unreachable set, `compare` could not raise `retraction-unverified`,
  and a run whose cached export had aged past its seven-day TTL printed a green
  `PASS` over a source nobody consulted — while `consulted` reported
  `retraction-watch: answered`.
- An Open Library outage no longer manufactures retraction doubt. `openlibrary`
  reached the unreachable set but carries no retraction signal, so an outage
  stated "retraction status not corroborated" against every book in a file.
- `--no-isbn`'s help text described a verdict the flag does not produce. See
  **Changed** above for the behaviour itself.
- Corrected the README's sample run, which predated the current renderer in its
  banner wording and in both its orderings, and its miscited arXiv footnote.
- A Retraction Watch outage no longer clears fabricated DOIs. Its outage joins
  the run-wide unreachable set, which `compare` also reads as "nothing could be
  reached", so every unresolvable identifier in a file turned into `UNCHECKED`
  and the run exited 0. Sources that hold no bibliographic record are now
  excluded from that branch.
- The CI matrix ran a single interpreter. `.python-version` pins 3.11 and
  nothing passed `matrix.python-version` to the toolchain step, so 3.12 and 3.13
  were never exercised despite the job names.
- **`--suggest` proposed nothing for an entry that does not start in column 1.**
  An entry's source span was located at the start of its first line while
  bibtexparser's `raw` starts at the `@`, so any leading whitespace failed the
  verbatim check and the entry was dropped from the span map without a word. A
  file exported with every entry indented got an empty suggestion, and one entry
  moved by hand got a suggestion missing exactly that entry. The fields added to
  an indented entry now also keep the file's own indentation, and its closing
  brace keeps its own.
- **A bibliography reached both from the command line and from a document's
  front matter was read twice.** Discovery resolves the paths it finds while the
  command line keeps what was typed, so the two compared unequal and every entry
  was collected twice. `_deduplicate` collapses on the identifier, so entries
  carrying a DOI survived it and the rest did not: the summary over-counted an
  arbitrary part of the file. Inputs are now compared as resolved paths, which
  also covers `bibaudit check notes/ notes/references.bib` and a `--bibliography`
  repeating a file already named.

[Unreleased]: https://github.com/lorenzoFabbri/bibaudit/commits/main
