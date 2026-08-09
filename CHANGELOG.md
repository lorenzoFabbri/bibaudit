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
  DOI-resolved entry's are. In free text the label has to open its line at
  column zero, which is where Zotero writes its own. MEDLINE back-matter pasted
  into the same box names other documents twice over — in prose (`Comment in:
  JAMA. 2003;289:2560. PMID: 12759325`) and at the start of the indented
  continuation lines `efetch` wraps a `RIN` or `CIN` block onto — and either,
  read as a declaration, resolves the entry to a correction or to a retraction
  notice instead of the work it cites.
- **A MEDLINE citation is compared on MEDLINE's own terms.** NLM files a serial
  under a title of its own making: the leading article dropped, a place
  qualifier appended, a subtitle written after a spaced colon — the sponsoring
  society on many journals, the title's acronym or a descriptive phrase on
  others.
  So `JT` is `Lancet (London, England)` where a bibliography stores *The
  Lancet*, and `Cancer epidemiology, biomarkers & prevention : a publication of
  the American Association for Cancer Research, cosponsored by …` where it
  stores the journal's own name — 26 of the 173 journals in a 300-record sample
  carry a subtitle, *J Clin Oncol*, *Clin Cancer Res* and *Ann Oncol* among
  them. `TA` is carried as an alternate title, so an entry storing `Lancet` gets
  an `info` note naming PubMed as the registry that holds it; a stored value
  whose opening `The`/`A`/`An` is the whole of the difference is a
  `REGISTRY-ARTIFACT`, and so is one matching everything before the subtitle,
  with or without an article NLM kept and the masthead does not — `The Journal
  of adolescent health : official publication of the Society for Adolescent
  Medicine` against a stored *Journal of Adolescent Health*. The qualifier is
  a `REGISTRY-ARTIFACT` on the same terms — `Annals of Medicine and Surgery`
  against `Annals of medicine and surgery (2012)` — because `compare` reaches
  the journal name only after the work has been pinned by its identifier, and
  a work appears in one serial: the qualifier disambiguates a catalogue, and
  no catalogue is being searched here. It comes off from its closing bracket
  back to the one that balances it, so a colon NLM writes *inside* one, as in
  `ASAIO journal (American Society for Artificial Internal Organs : 1992)`, is
  neither read as the subtitle separator nor split on. None of it is a prefix
  test: `Cancer Epidemiology` is a different journal and still fires. Dates
  the same way: `DP` is the issue a citation is filed under and
  `DEP` the day the work went online, and both reach `Record.years`, so an entry
  citing the online-first year of a paper printed the following year is accepted
  exactly as Crossref's `published-online` already was.
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
  since stopped answering is invisible from this side. So it establishes that
  the DOI resolved to a citation the stored PMID does not name, and not that the
  stored PMID names anything else. It takes `--verbose` to see on a default run,
  or `--fail-on INCOMPLETE`, which prints it as well as failing on it. Two ways
  it declines to fire:
  `Record.pmid` is left unset when PubMed answered for the DOI under more than
  one PMID, so an entry storing either is never accused; and a stored number
  that is the record's own PubMed Central accession — NLM issues both for one
  article and Zotero keeps them on adjacent `Extra` lines — is a
  `REGISTRY-ARTIFACT`.
- **A retraction source that was never asked is named, not passed over.** A
  reference that reaches fewer than every source carrying the signal now carries
  a `status/not-asked` finding naming the ones it did not reach, and the run
  prints it beside the banner as it prints an outage, on its own line ending
  `not asked`. `consulted` names `crossref`, `datacite`, `pubmed` and
  `retraction-watch` on every reference in every run, so an unasked source reads
  as `not-asked` rather than as a key that is not there. What that comes to on a
  reference resolved by its **PMID**: it keeps both of PubMed's own signals —
  NLM's `PT - Retracted Publication` and its `ECI` cross-reference, fields of the
  MEDLINE record the lookup already returned, so a retraction NLM indexed fails
  the run and a concern NLM recorded is reported as a concern, and a record
  carrying both — the ordinary escalation, and the shape of the Wakefield and
  Surgisphere papers — is reported as the retraction, exactly as it is when the
  same record is reached by its DOI — and it is asked
  neither of the two that take a DOI, so the finding names `crossref,
  retraction-watch`. A book resolved by its ISBN reaches none of the four and
  names three. A run given `--no-retraction-check` names `retraction-watch`, and
  loses the `ECI` reading as well without a line saying so, because PubMed
  answered for the citation; `docs/retraction.md` states that gap instead.
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

- **Every PubMed citation a DOI resolves to is fetched, not one of them.**
  Where `esummary` attributed two PMIDs to one DOI, only the number that sorted
  last was fetched, and its `PT` decided the entry's retraction status — so a
  work PubMed records as retracted under one citation and not the other
  reported clean. Both are now fetched and the tie breaks towards the finding.
  Which citation supplies the title and byline is still arbitrary and
  `Record.pmid` is still withheld: two citations of one work agree on those.
- **`status/not-asked` says which of its two reasons applied.** "Were never
  asked" covered a source that takes an identifier the reference does not carry
  — which no rerun changes — and one that had a key and was left out by
  `--no-retraction-check`, which dropping the flag fixes. The note now names
  the sources in each group separately: `crossref, retraction-watch take a DOI
  this reference does not carry` against `retraction-watch was not queried on
  this run`.
- **A volume or issue number is no longer a mismatch for its leading zero.**
  Crossref deposits `"issue": "05"` where MEDLINE writes `IP - 5` for the same
  work, so an entry exported from either failed against the other — and on the
  PMID path the other one is the only value there is. It is a
  `REGISTRY-ARTIFACT` reading `one side writes the number with a leading zero`,
  which blames neither. `Volume 18` against `18` is untouched: that is a label
  copied into a numeric field, and the fix is one the user can make.
- **`TI - [Not Available].` is read as an empty field, not as a title.** NLM
  writes that placeholder where it holds no English title for an article
  published in another language, on 66,776 citations. Compared as a title it
  scored 0.14 against the entry's own and reported `title/wrong-work` — one
  weak byline away from accusing a correct entry of citing a different paper.
  The record's own `TT`, the transliterated title, is read instead, and it is
  the title such a bibliography stores.
- **A consortium the byline credits and MEDLINE files apart no longer shifts
  the whole author list.** Crossref credits a consortium as an `<organization>`
  inside the `author` array; MEDLINE files it under `CN` and lists only people
  under `FAU`/`AU`. `names._interleaved_collectives` covered the same defect
  from the other direction only, so an entry exported from Crossref carried a
  creator the MEDLINE byline does not and reported a substitution at that
  position and every one after it — `#6 for the ITC Project Collaborators`
  against `#6 Kress, Alissa C` on PMID 42552006, and 25 consecutive positions
  on another entry. It is now a `REGISTRY-ARTIFACT` naming the organisations,
  on the same evidence its mirror requires: what is left after the collectives
  come off must align exactly with the registry's list.
- **A verdict of `OK` can no longer be reached over zero comparisons.** Every
  check in `compare` returns in silence when the registry's value is empty, so
  a record holding no title, byline, year, container, volume, issue, pages or
  publisher produced no issues at all and the entry reported `OK` — "every
  checked field agrees", over nothing. That entry is now `UNCHECKED` with an
  `identifier/uncompared` finding at `info`, which ranks below every other
  verdict and so can only displace one reached over an empty comparison.
- **A MEDLINE book record is compared, not skimmed.** NLM files a book's title
  in `BTI` rather than `TI`, its editors in `FED`/`ED` rather than `FAU`/`AU`,
  its structural type in `PT`, and the dates a chapter was contributed and last
  revised in `CTDT` and `DRDT` beside the series' own `DP`. None was read, so
  PMID 20301295 — the *GeneReviews* volume — produced a record with no title,
  no byline and no container, an entry with all three fabricated was compared
  against none of them, and the run reported `OK`. A chapter now keeps its own
  title and gains the volume as its container, an edited volume's byline is its
  editors (the fallback Crossref's client already makes), and any year the
  record carries is accepted rather than the series' start year alone. `PB`
  stays unread: `compare` does compare a publisher, MEDLINE writes the place of
  publication into that field, and nothing in `benign.py` absorbs it.
- **A surname is no longer split in two by a letter that does not decompose.**
  `fold` reduced a precomposed letter to its base and dropped the mark, but
  `ß`, `æ`, `ø`, `ł`, `ð`, `þ`, `đ`, `ħ` and `ı` are not a base plus a mark and
  became a **space** — `Straße` folded to `stra e`, `Kjær` to `kj r`. MEDLINE
  romanises a byline and Crossref deposits it as the author writes it, so the
  two registries disagree on these names systematically rather than
  occasionally, and a correct entry failed on `authors/mismatch`. Those letters
  now fold to the spelling CLDR's `Latin-ASCII` transform gives them, and a
  letter with no entry there is removed rather than spaced, so none of them can
  break a name into fragments. Titles and journal names are compared on the
  same key and gain the same fix; a title in a non-Latin script still folds to
  nothing, as before.
- **A source bibaudit reads no retraction signal from is neither a witness nor
  a dissenter.** Europe PMC and OpenAlex were added to the identifier-less
  search path without being excluded from the retraction notes, so an outage at
  either printed "retraction status not corroborated" naming a source that
  would have carried nothing had it answered — on an entry whose whole
  candidate pool is those two plus Crossref, the doubt was stated on all three.
  In the other direction the `status/retracted` note listed any source that
  answered and recorded nothing as dissenting, without consulting that
  exclusion at all: even DataCite, named in it since it was written, printed
  "and not by datacite, which answered for this work and carries no retraction
  linkage" beside a confirmed retraction. The exclusion is now derived from the
  registry clients in the test suite, so a client that sets no
  `Record.retracted` and is not named there fails the build.
- **A registry defect is judged against the record that supplied the value.**
  `compare` lets the corroborating registry fill a field the primary left
  empty, but handed `benign.classify` the primary's record regardless — so a
  Crossref deposit carrying no `container-title` put PubMed's `JT` on the
  right-hand side while a Crossref record holding neither `JT` nor `TA` was
  asked to explain it. `The Lancet` against `Lancet (London, England)` reported
  `container/mismatch` and failed the build on every DOI-resolved entry, the
  case `_container_leading_article` exists to prevent. The suppression's
  `source` column names that registry too, rather than the primary that
  supplied nothing.
- **`--fail-on` now decides which reference groups are printed, not only the
  banner.** The group filter read the default failing set rather than the policy
  in force, so a run told to fail on a verdict outside that set exited 1 over a
  summary count with no citekey, no locator and no issue line — nothing to act
  on, while the finding itself was visible only under `--verbose`, which fails
  nothing. A verdict named in `--fail-on` now brings its references with it, and
  one excluded from it keeps printing.
- **An `et al.` written as a Zotero creator no longer counts as an author.**
  Zotero's single-field creator — `fieldMode` 1 in the database, `name` in its
  item JSON, `literal` in CSL — carries both a corporate byline and the
  truncation marker a producer that stores only the first author writes for the
  rest. Counted as a name it made every such entry disagree with the registry
  on author count, which `compare` hands to the verdict as evidence the
  identifier resolved to a different work. It is now read as truncation on all
  three routes, which voids the length comparison and nothing else — a wrong
  first author beside the marker still fires.
- **A wrong year on an entry no Crossref record answered for is reported
  again.** The deposit-timestamp suppression excuses a registry year three or
  more years later than the stored one when no print date corroborates it, and
  Crossref is the only registry that writes a print date at all — so on a
  PubMed, DataCite, Open Library or search-confirmed record the condition held
  vacuously and every such entry three or more years early was filed as a
  registry defect and dropped out of the default report. The rule now applies
  to Crossref records only.
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
