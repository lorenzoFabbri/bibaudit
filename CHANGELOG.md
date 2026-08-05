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

- **A PMID is read as an identifier in its own right, and checked.** BibTeX's
  `pmid` field, an `eprint` paired with `eprinttype = {pubmed}`, a labelled
  `PMID: 28520842` line in a Zotero `Extra` or a CSL `note`, and CSL's own
  `PMID` variable all reach `Reference.pmid`. An entry carrying a PMID and no
  DOI is resolved through PubMed's `efetch` alone — one request where a DOI
  costs three — rather than searched by title and author, so it can now report
  `BAD-ID` (PubMed answered and holds no such record) or `UNCHECKED` (PubMed
  unreachable, or `--no-corroborate`, so nothing was asked). An entry carrying
  both is resolved by the DOI and its PMID becomes a compared field: one naming
  a different citation than the DOI resolved to is a `FIELD-MISMATCH`, reported
  as `pmid/mismatch`, and `compare.CHECKED_FIELDS` gains `pmid` accordingly.
  `Record.pmid` carries the registry's side of that comparison; it is left
  unset when PubMed answered for the DOI under more than one PMID, so an entry
  storing either of them is never accused. Nothing is ever proposed for a
  missing PMID: `--suggest` fills absent fields, and this check never reports
  one as absent. Two consequences worth knowing before re-running an audit: a
  PMID-bearing entry is no longer searched for by title and author, so it no
  longer receives a proposed DOI from a search candidate; and it reaches one of
  the four retraction sources rather than all four, keeping NLM's `PT -
  Retracted Publication` — a field of the MEDLINE record the lookup returns —
  and not Retraction Watch's export, Crossref's `updated-by` or PubMed's `ECI`
  cross-reference, each of which is queried by DOI. An expression of concern
  about such an entry therefore goes unreported.
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
