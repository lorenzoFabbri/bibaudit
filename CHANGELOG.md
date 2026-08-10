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
  `RetractionOutage`, a `Transient` carrying a whole `RetractionStatus` — every
  notice the sources that *did* answer produced, beside the names of every
  source the outage took down rather than only the leg that raised.
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
- **MEDLINE's corporate byline is read.** `CN` is where NLM credits a
  consortium, a society committee or a writing group, and for some citations it
  is the whole byline: PMID 42538063, a committee opinion in *Fertility and
  Sterility*, is credited to `CN - Practice Committee of the American Society
  for Reproductive Medicine` and carries no `FAU` or `AU` line at all, as do 4
  of 3,000 citations in a live sample. Unread, those records reached the
  comparison with no creators, which returns before comparing anything, so an
  entry whose byline was three invented people was reported `OK`. It is now one
  collective creator — never split, never parsed as a person, since a corporate
  name need carry no word this project would recognise (`Frontiers Production
  Office` arrives as a surname *Office*) — and an entry naming people instead
  is a `REGISTRY-ARTIFACT` line stating what the record credits. `CN` beside a
  personal byline stays unread, 26 of those 30 sampled citations: the position
  it belongs at is what a comparison would need, and the record does not carry
  one.
- **A citation crediting a different person of the same surname now fails.**
  The byline comparison stopped at the surname key, so `Wade, Zbigniew` against
  a registry's `Wade, Nicholas` was agreement, with nothing recorded at any
  verbosity — `--show-suppressed` could not recover it, because nothing was
  suppressed. Forenames are now compared, by their first initial and only where
  both sides supply one, and a disagreement is `authors/forename` at error
  severity, naming both people. Measured over 3,963 MEDLINE/Crossref pairs of
  the same work fetched live across 51 publication years: replacing one
  creator's forename with an incompatible one is reported on 3,652 of 3,693
  entries, against none before, and the two registries' own unmutated bylines
  gain a finding on 3 of 3,876 (0.077%) — one of them a false alarm, the other
  two the registries genuinely disagreeing about who is credited. Everything
  registries disagree about legitimately is excused and written up in
  [registry defects](registry-artifacts.md): an initial against the name, a
  middle initial one side omits, initials run together against initials
  separated, hyphenation and accents, mojibake, a forename in a script `fold`
  discards, and a compound surname the two sides divide differently.
- `CONTRIBUTING.md`, `SECURITY.md`, a Dependabot configuration, a coverage floor
  (`fail_under = 92`, with branch coverage on), and a tag-triggered release
  workflow using PyPI Trusted Publishing.

- **Every file in `tests/data/` has to say where it came from, and the build
  fails for one that cannot.** `tests/data/PROVENANCE.toml` records, per file,
  the registry, the identifier the response was requested under and that
  request's URL, and `tests/test_fixture_provenance.py` fails on a file with no
  entry, an entry with no file, an entry whose identifier is not the one inside
  the file, or a URL no client in `src/bibaudit/registries/` would issue. Those
  checks establish that a claim hangs together and nothing more — a MEDLINE
  citation written around a live PMID satisfies every one of them, and four of
  the ten fabricated fixtures this gate was built after were exactly that. `uv
  run pytest -m network` re-fetches every URL and diffs the answer against the
  bytes on disk, which is the only thing here able to tell a recorded registry
  response from a written one; it now reaches Crossref, DataCite, PubMed and
  Retraction Watch, where before this it reached none. The procedure for adding
  a fixture is in `CONTRIBUTING.md` and its third step is not automated.

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

### Removed

- **`Reference.arxiv`.** No adapter ever set it, and `Reference.identifier`
  ranked it above `isbn` — so had one, every entry carrying an arXiv id would
  have been reported `BAD-ID`, "resolves in no consulted registry", about a
  value no registry is ever asked for: Crossref and DataCite take a DOI,
  PubMed a DOI or a PMID, Open Library an ISBN. That property is what the
  field's removal restores, and the identifiers a reference may be resolved by
  are exactly the three the documentation already named. An arXiv preprint is
  still reached the way it always was, through the DOI its repository minted
  (`10.48550/arXiv.1706.03762`), which DataCite answers for.
- **`adapters.markdown.QuartoScan` and `adapters.markdown.scan_quarto`**, two
  aliases of `MarkdownScan` and `scan_markdown` carried in `__all__` under an
  older, Quarto-only spelling and described as kept so that existing imports
  would not break. Nothing could have imported them: the names appear nowhere
  in this repository's history, no version has been released, and a first
  release that ships a deprecation deprecates nothing. The module's public
  names are the two functions and the one result type its docstring promises.

### Fixed

- **A creator name carrying one letter from another script is compared as the
  name.** A publisher deposits a forename or a surname with a Cyrillic or Greek
  letter where its Latin lookalike belongs, and both registries inherit it:
  creator four of `10.26442/00403660.2024.07.202907` has a forename opening on
  CYRILLIC CAPITAL LETTER TE, and NLM's XML for the same paper (PMID 39106512)
  carries that same code point. `fold` had no romanisation for such a letter
  and deleted it, which leaves a comparison key one letter *shorter* rather
  than empty — and short reads as knowledge where empty reads as ignorance.
  The surname was compared missing a letter, and the forename's second letter
  was read as its first. Both directions were wrong at once: an entry spelling
  the forename *Tatiana* was reported `authors/forename`, and one spelling it
  *Anna* was cleared. Such a letter is now read as the Latin letter it is drawn
  as, from Unicode's own UTS #39 confusables table, and only where the value
  carries a Latin letter of its own and every non-Latin letter in it has a row
  — so a name a registry deposits in its own script still has no comparison key
  and is still reported as one this tool cannot express. 23 of the 91,226
  creators in a 32,000-work random Crossref sample carry one; 19 are repaired.
  Lower-case Greek is deliberately excluded, being this literature's notation
  rather than a substitution: `TNF-α` and `IFN-γ` fold exactly as before.
  `docs/limits.md` states what is left.

- **The character NLM spells out because its format is ASCII is read back.**
  `medline` cannot emit a character outside ASCII, so it writes the character's
  own Unicode name instead, with the script moved to the end: PMID 40778922 is
  `FAU - Panferov, capital A, Cyrillic S`. That phrase carries a comma, and
  `FAU` is `Surname, Initials`, so the byline split at the wrong one and the
  forename became `capital A, Cyrillic S` — initialling on `c`, which reported
  `Panferov, A. S.` and cleared `Panferov, Carl S.` The same citation's XML
  carries `<ForeName>&#x410; S</ForeName>`, so the rendering is put back to the
  character NLM holds, on every field rather than the byline alone.
  `unicodedata.lookup` is the whole of the guard: a phrase it does not know is
  left exactly as found.

- **An entry whose verdict is quiet but whose finding is not is printed.** The
  terminal report chose which groups to show by verdict alone, so an
  error-severity issue riding on a verdict outside that set was dropped with
  its entry. A run in which Crossref, DataCite and PubMed all time out while
  Retraction Watch answers reaches `UNCHECKED` carrying `status/retracted` —
  deliberately, since calling the entry `RETRACTED` would assert that the work
  Retraction Watch logged is the work this reference cites — and printed
  `UNCHECKED 1` beside `errors by field  status=1`, an error counted with no
  citekey, no locator and no field. The JSON report carried the issue all
  along, so the two reports disagreed on the one field whose miss puts a
  retracted paper in a manuscript. An outage on its own is still not printed
  per entry: the filter is loosened for errors, not abandoned.

- **A PMID batch whose answer named no citation no longer reports the numbers
  in it absent.** `by_pmids` attributes each MEDLINE block by the block's own
  `PMID` line. A block carrying none was skipped silently, so its batch's
  numbers fell into the state that means "`efetch` answered and holds no
  citation under them" — the whole of the evidence a `BAD-ID` on a PMID rests
  on. An answer nobody could read was being reported as an answer that the
  citation does not exist. The batch is inconclusive now, as it already was for
  a 404 on `efetch.fcgi` itself.

- **The publication type NLM puts on a retraction notice is named as the one it
  emits.** Six statements in shipped source and test docstrings said, in the
  present tense, that PMID 20137807 carries `PT - Retraction of Publication`,
  and a `tests/data/` fixture presented as a real MEDLINE response carried it.
  NLM renamed MeSH descriptor D016440 in 2025: the record carries `PT -
  Retraction Notice` and nothing else, `"Retraction of Publication"[pt]`
  matches no record in PubMed, and the two spellings the current vocabulary
  does answer for are `Retracted Publication` (33,710 citations) and
  `Retraction Notice` (32,753). No verdict moves — `pubmed._PT_RETRACTED` is
  exact fold-equality against the retracted paper's own type, so both spellings
  of the notice's type were safely non-matching — but the direction rule was
  being proved against a snapshot that had drifted, and the fixture is now
  refetched verbatim with its `PT` list pinned. Opposite in meaning is not
  exclusive in fact, which the file also now says: 488 citations carry both
  types, and on those the answer to "was this record's own article retracted"
  is yes.

- **A creator the entry names past the registry's last is reported.**
  `names.compare_author_lists` walks two bylines in step and stops at the
  shorter one, so a name appended to the end of an entry's byline was compared
  against nothing and the only trace was an author-*count* warning, which does
  not fail. Appending one fabricated name to an otherwise correct byline
  produced no failing verdict on **4,255 of 4,255** live entries — the
  documented failure mode of a generated bibliography, and the reason
  `compare._check_authors` compares the whole list rather than the first
  author. It is now `authors/uncorroborated` at error severity, one line per
  creator, naming the creator, because a count line names nobody and the
  reader's question is *which* name no record carries. Appending a fabricated
  name to each of 1,177 entries written from the MEDLINE record now fails every
  one of them, and the same 1,177 unmutated fail none.

  A registry whose byline is short at the tail produces the identical shape and
  no author list can separate the two, so three kinds of tail position are kept
  out of the claim: a **collective** creator, since an organisation is not an
  invented co-author (PMID 38236418, `on behalf of the STAAB consortium`); a
  **surname the registry's own byline carries elsewhere**, without which the
  line contradicts the record it quotes; and a creator the **corroborating**
  registry names, since *uncorroborated* is a claim about every record that
  answered while only one byline is ever compared. After those, the cost on
  3,040 works whose MEDLINE and Crossref records were both fetched is 7
  (0.23%), all of them on the PMID path, where there is no second witness;
  on the DOI path it is none.

- **A byline that lost an author is no longer excused as a reordering.** The
  escape tested set membership of surnames position by position, so a byline in
  which a surname repeats — routine in Chinese, Korean and Japanese author
  lists — read a whole one-position shift as an exchange: 104 of 2,551 live
  entries missing their first author came back non-failing, 94 with every
  shifted position excused. `docs/registry-artifacts.md` promised "a genuine
  exchange, not a name that merely went missing", and the code did not
  implement it. Both bylines must now hold the **same creators counted**, which
  makes a length difference impossible. Of 3,040 live pairs, 9 carry a
  `reordered` position; the 3 that lose it are the 3 whose lengths differ. A
  reordering compounded with a spelling difference is now reported.

- **Two page locators neither side can read no longer agree with each other.**
  `first_page` read an optional letter, any zero padding and the digits, and
  returned `""` for anything else — and `""` compares equal to `""`, so every
  unreadable locator agreed with every other one. A bibliography storing
  `NP585-NP599` against a record holding `NP580-NP599` came back `OK`. SAGE's
  online-only `NP…` numbering and Roman front matter are the live shapes: 24 of
  4,548 MEDLINE `PG` values (0.53%) in a fresh 4,800-citation sample spanning
  1992-2026, 22 of them `NP…`, `i-xv` on PMID 38284210 and `suppl 4 p.` on PMID
  10118706. An unreadable locator now falls back to the folded text ahead of
  the range separator, and `compare._check_pages` accepts an empty opening only
  against identical text.

- **A MEDLINE `FAU` written without its comma is read MEDLINE's way.** `FAU` is
  `Surname, Initials` and the comma says which half is which; without it the
  value reached `names.parse_name`, whose comma-less convention is BibTeX's
  "Given Family", and `Okano J` arrived as a creator surnamed `J`. A registry
  surname of one character is what `Reason.REGISTRY_INITIAL_ONLY` accepts *any*
  stored surname against, so the entry's byline stopped being checked at that
  position. 13 of 24,456 `FAU` values on 11 of 4,800 citations carry no comma,
  each written character-for-character as its own `AU` line. The one citation
  written the other way round — PMID 31128948's `K Sikorska`, where
  `"Sikorska K"[au]` answers 215 citations and `"K Sikorska"[au]` exactly one —
  is rewritten on two conditions, because 67 of 48,919 `FAU`/`AU` values open
  with a single letter and the rest are surnames of one or two letters followed
  by their initials (`S DMTS`, `A LK`, `N AK`, `T T`), which is NLM's order
  already.

- **A PubMed outage no longer deletes Retraction Watch's answer.** The outage
  raised a `RetractionOutage` carrying the names of the downed sources, and
  everything Retraction Watch had already said died with the frame — so a DOI
  Retraction Watch records as retracted and Crossref does not linked read
  `OK` / `retraction-unverified` / exit 0 during any NCBI hiccup, with
  `consulted` reporting `retraction-watch: answered` and the note asserting
  that no registry which answered records a retraction. 8 of 300 randomly
  sampled Retraction Watch retraction DOIs carry no Crossref `updated-by`
  linkage at all, which is ~1,600 DOIs across the export where that was the
  whole of the evidence. The exception now carries a whole `RetractionStatus`,
  built by the same merge the clean return uses, and both call sites read it
  through `audit._outage_status`.

- **The Retraction Watch export is fetched past the shared registry cache, so
  its seven days bound.** The index has a seven-day TTL, but the fetch behind
  it went through the ordinary registry cache on `--cache-ttl-days`, default
  90 — so when the index expired the "refetch" was served from a body up to 90
  days old, and a retraction logged in that window read clean on the one source
  that exists to catch what Crossref and NLM do not. `Client.get_text` gained
  `bypass_cache`, detaching the store on the read side, on the body write and
  on the 404 marker, and the 64 MB `json.dump` of the CSV into the cache is
  gone with it. `docs/retraction.md`, `docs/cli.md` and `docs/ci.md` stated the
  rule the code did not implement, and now state the one it does.

- **A suppression on a stored PMID names the registry that carries both
  numbers.** `_check_pmid` reads its right-hand value off whichever record
  holds a PMID, which is never the primary of a DOI-resolved entry — Crossref
  and DataCite set `Record.pmid` on nothing — but the `REGISTRY-ARTIFACT` line
  beside it was attributed to the primary, so "stored number is this record's
  own PMC accession" printed under Crossref's name, inviting a reader to check
  the claim against a record with no `PMC` field. The mismatch branch in the
  same function was already right.

- **A zero-padded article number is one.** `is_article_number` counted the
  value after `first_page` had normalised the padding away, and `085001` and
  `85001` are both five digits there — so neither cleared the six-digit floor,
  `benign._pages_article_number` never fired, and three correct entries in one
  1,923-entry live sweep (PMIDs 42571480, 42571556, 42571506, *J Biomed Opt*,
  `PG` `085001`/`086003`/`086004` against a Crossref `page` of
  `1-15`/`1-16`/`1-37`) were reported `pages/mismatch` and failed the build.
  Both floors now count the value as the source wrote it, padding included,
  because the padding is the evidence: no journal files a page number with
  leading zeros. 177 of 4,548 MEDLINE `PG` values in a fresh 4,800-citation
  sample are padded numerics and every one of them is six characters written.
  The predicate reads notation and not the article behind it, so the same
  number written bare stays under the floor — a residual with no witnessed
  instance, and stated in `docs/registry-artifacts.md` alongside the other one:
  once either side looks like an article number the other is not examined at
  all.

- **The "not asked" note names each source beside the key it is looked up by.**
  The keyless sources were pooled into one clause, so a book carrying neither
  identifier read `crossref, pubmed, retraction-watch take a DOI or a PMID this
  reference does not carry` — true of PubMed, and loose about the other two,
  which are never asked with a PMID. The note also no longer states a DOI as a
  limit of Retraction Watch's: 33,403 rows of the 2026-08-09 export carry the
  original paper's PMID, and indexing the export by DOI alone is this tool's
  choice.

- **`--cache-dir` reaches the Retraction Watch index.** It was built under the
  default cache root whatever the run was given, so `bibaudit cache info`
  under-reported by the whole index and `bibaudit cache clear` left it in
  place — and with a seven-day TTL that `--refresh` does not shorten, a run
  that cached a bad index had no route back from the command line. The
  directory is unchanged for a run that does not pass `--cache-dir`.

- **A `RetractionNature` this build cannot rank is announced rather than
  dropped in silence, on every run and not only the one that parsed.** Such a
  row is still skipped — guessing "retraction" for a category that may be
  milder is the false alarm the third rule exists to prevent — but the run now
  warns, naming the value and its row count, because skipping is a missed
  notice on the one field where a miss has no remedy and nothing else in the
  run mentioned it. The warning was raised inside the CSV parse, which a run
  reaches only on a cache miss, so for the life of the index every later run
  was silent about a row it had skipped. The counts are cached beside the
  notices and said once per process from the one point both routes to an index
  pass through; the payload gained a shape, so an index written by an earlier
  build is refetched rather than read back as one that skipped nothing. Every
  value in the 2026-08-09 export is recognised, so an ordinary run is silent.

- **A retraction whose date Retraction Watch left blank is no longer withdrawn
  by a reinstatement.** An unreadable date sorted as the earliest date there
  is, so a notice carrying one counted as "dated at or before" every
  reinstatement and was dropped — the inverse of the rule the same function
  keeps on the reinstatement's side, where an unreadable date withdraws
  nothing. 241 rows of the 2026-08-09 export carry no date.

- **Each retraction source is reported under the kind it recorded.** Two
  sources' notices about one DOI were merged to the more definitive kind and
  that kind was then stamped on both, so a work Retraction Watch logs a
  *correction* for and NLM records as retracted printed
  `pubmed=retraction; retraction-watch=retraction`. The retraction is still
  reported — `compare` takes the union across sources — but the correction is
  now attributed to the source that logged one.

- **A retraction is stated even on a DOI no bibliographic registry resolved.**
  Retraction Watch's export is keyed on the original paper's DOI whether or not
  Crossref, DataCite or PubMed carry it — 3 of a random 400 of its retraction
  DOIs resolve in none of the three — and the notice was dropped for those,
  leaving a report that said the identifier was bad and nothing whatever about
  the retraction. The verdict is unchanged (`BAD-ID`: the identifier still
  resolved nowhere, and the work Retraction Watch logged cannot be confirmed to
  be the work cited), and a status source still cannot make an identifier look
  resolved.

- **A Retraction Watch export that carries no usable row is reported as an
  outage**, not as a database with nothing in it. A 404 on the bulk endpoint —
  which has already moved once under this tool — and a 200 whose body is a
  maintenance page, a rate-limit notice or a truncated download both reach the
  parser as zero rows, and both were read as "Retraction Watch answered and has
  no retractions". The source that exists solely to carry this signal then
  reported no gap, and the empty index was written to a cache with a seven-day
  TTL that `--refresh` does not reach, so one such fetch answered a week of
  healthy runs.

- **A correct journal abbreviation is no longer failed for a qualifier NLM
  appends to it.** The rule that drops NLM's trailing parenthetical ran on the
  record's primary container title alone, while `MedAbbr` carries the same
  parenthetical and reaches the record as an alternate title: `Acta
  Hepatogastroenterol` against `JT - Acta hepato-gastroenterologica` and
  `TA - Acta Hepatogastroenterol (Stuttg)` (NlmId 0340734) matched neither, and
  nothing else reaches the pairing. Measured through the real comparison over
  NLM's own serial list, 1,075 of the 2,695 serials with a qualified
  abbreviation reported `container/mismatch` against a bibliography storing the
  abbreviation as ISO 4, Web of Science and Scopus write it; 67 still do. The
  reduction now runs on every name the record carries, and where the match came
  off one of them the reason says so: `registry appends a parenthetical
  qualifier to another name it carries for the journal`. On those names the
  remainder must keep two tokens — one word of an abbreviation is one truncated
  word, `Proc (Bayl Univ Med Cent)` leaving `Proc`, which opens 442 serials'
  abbreviations and is the whole of none of them — and no leading article comes off them,
  since `An` there opens *Anales* rather than a byline in English. What the
  widening clears and what the floor does not remove are counted and named in
  `docs/registry-artifacts.md`.
- **An entry crediting the organisation the registry credits is no longer
  suppressed.** The two collective escapes exist for a group name standing
  against a list of *people* — one side names the consortium, the other its
  members — and they fired ahead of the comparison whenever either side held a
  single collective creator, so a byline naming the same organisation the
  record does, character for character, was reported `REGISTRY-ARTIFACT` under
  `collective author`. It is now compared and agrees. Two *different*
  organisations keep the suppression: one name against one name leaves the
  positional comparison nothing to work with either way.
- **A suppression that edited both sides now says so.** Taking a journal's own
  acronym off the front of a stored name is matched against the names the
  record carries *and* against two reductions of them — NLM's spaced colon and
  its trailing parenthetical — so `JACCP: JOURNAL OF THE AMERICAN COLLEGE OF
  CLINICAL PHARMACY` against `JT - Journal of the American College of Clinical
  Pharmacy : JACCP` (PMID 42522049) lost the acronym from one side and
  ` : JACCP` from the other under a reason naming only the acronym. Where the
  registry's value was reduced to reach the match the reason now ends `, and
  the registry adds its own subtitle` or `, and the registry adds a
  parenthetical qualifier`.
- **A modifier letter folds like the apostrophe it stands in for.** Removing
  every non-ASCII letter with no romanisation, added so an unmapped letter
  could never split a surname, also removed U+02B9 MODIFIER LETTER PRIME and
  the two turned-comma glyphs ALA-LC romanisation writes for a soft sign or a
  glottal stop — while the ordinary apostrophe a keyboard and a reference
  manager write still became a space. Two spellings of one name became two
  comparison keys: Crossref's own title for 10.15862/24sats419 spells the
  surname with U+02B9 and an entry writing `Vasil'ev` folded to `vasil ev`
  beside the deposit's `vasilev`. They agreed before the romanisation map
  existed, and agree again.
- **A different journal is no longer cleared as an abbreviation of the one
  cited.** `_container_abbreviation` required the stored words to be in-order
  prefixes reaching the registry name's last word, and skipped over anything
  in between — so `Annals of Oncology` against `Annals of surgical oncology`,
  `Journal of Cancer` against `Journal of gastrointestinal cancer` and `Cancer`
  against `Pediatric blood & cancer` were each reported `REGISTRY-ARTIFACT`
  with `stored name abbreviates the registry name` beside them, and the run
  exited 0 on a bibliography naming a journal the paper did not appear in.
  Over the 37,989 serials in NLM's own list the rule accepted 27,851 ordered
  pairs of serials with different titles as abbreviations of one another. A
  skipped word may now be three characters at most — every word ISO 4 deletes
  is an article, a conjunction or a preposition, and 25,758 of the 26,500 words
  skipped across NLM's 25,641 abbreviated titles are that short — which leaves
  903 of those pairs.
- **A journal name the registry files under a leading article is reachable
  from the masthead form.** The rule took the article off the stored side
  only, so an entry storing *Canadian Journal of Statistics* failed against
  `JT - The Canadian journal of statistics = Revue canadienne de statistique`
  (PMID 42559441) — the article-free form is what Crossref deposits and what a
  bibliography exports, and 62 of the 63 parallel titles in NLM's list whose
  half opens with an article reported `container/mismatch` for it. One article
  now comes off one side per comparison, either side, and the reason printed
  says which; `A Journal of Cancer` against `The Journal of Cancer` is still a
  difference. `Lancet` against `The Lancet` is reported as the article it is
  rather than as `stored name abbreviates the registry name`.
- **A work its registry files under two types is compared against both.** NLM
  writes MEDLINE's `PT` list alphabetically, so the type leading a citation is
  not the structural one: PMID 42557261, a data descriptor in *Scientific
  Data*, is `PT - Dataset` then `PT - Journal Article`, and the record said
  the work is a dataset and not an article. A correct `@article` citing one
  moved off `OK` to `INCOMPLETE`, into the summary counts and the JSON, and
  failed under `--fail-on INCOMPLETE`; `"dataset"[pt] AND "journal
  article"[pt]` returns 5,670 citations. Any type the registry itself carries
  is now acceptable, on the same terms as any year and any container title it
  carries, and a `kind/incompatible` finding names every type the record
  holds rather than only the first.
- **A concern or a correction printed beside a retraction no longer says the
  work stands.** Two sources reporting different kinds for one work is
  ordinary: of the 650 DOIs whose strongest Retraction Watch row is a
  correction, 19 carry a Crossref `updated-by` retraction. Both findings are
  still printed, because they are separate statements and all of them are
  true. What changed is the milder note's
  closing clause: where a retraction is recorded for the same work it now reads
  `It does not undo the retraction recorded for this work` instead of the work
  standing and the citation being legitimate. On its own, each note is
  unchanged.
- **A correction published after a retraction no longer downgrades it.**
  Retraction Watch logs a DOI as often as its status is restated, and the row
  with the latest date decided the finding — safe while every row meant
  "retracted", and a downgrade once a correction became an `info` finding of
  its own. 52 DOIs in the 2026-08-09 export are indexed differently by the two
  rules: 47 carry a correction dated strictly later than every retraction row,
  one more of the same date, and four a later expression of concern. Crossref
  independently flags 51 of the 52, so on that export no verdict moves; the
  exception is `10.1002/ana.24658` (retracted 2016, corrected 2019), which
  carries no Crossref `updated-by` and today resolves in no registry at all.
  The strongest notice for a DOI now wins, not the
  newest: a work that has ever been retracted is retracted, and a correction
  published afterwards amends the notice rather than the withdrawal. A
  `Reinstatement` is still read by date — it withdraws every notice dated at or
  before it, and leaves a later one standing.
- **A corrected paper is reported as corrected, not as retracted.** Retraction
  Watch's export carries a `RetractionNature` of `Correction` beside
  `Retraction` and `Expression of concern`, and every kind outside the concern
  vocabulary counted as a retraction — so an entry citing
  `10.3390/nano14090769` (*Nanomaterials*), for which Retraction Watch logs a
  correction and nothing else, printed `RETRACTED — the cited work has itself
  been retracted` with the word `correction` in the registry column and exited
  1. It is now a third finding, `status/correction`, at `info` severity: the
  work stands and has been amended, the verdict does not move, and the note
  says the corrected version is the one to read the numbers off. It reaches the
  reader through the JSON report and `--verbose`, and no `--fail-on` makes it
  bite, because it produces no verdict of its own. Where two sources report on
  one DOI a correction now ranks last, below an expression of concern it had
  been softening.
- **A serial's parallel title is a container the registry carries.** NLM joins
  one journal's two names in `JT` with a spaced equals sign — `Journal of
  preventive medicine and public health = Yebang Uihakhoe chi` — on 607 of the
  37,989 serials in its own list. Both halves are the journal's own name and a
  bibliography stores whichever its house style uses, so each is offered as a
  `container_alternates` entry and matching one is an `info` note naming
  PubMed, exactly as `TA` already was.
- **A journal's own acronym written ahead of its name is a `REGISTRY-ARTIFACT`.**
  Several publishers set the masthead as `JNCI: Journal of the National Cancer
  Institute` and a reference manager copies it whole; NLM never files a serial
  that way, so four entries in a 386-entry live sample failed on a token the
  registry's name does not contain. The prefix comes off only when its letters
  are word-initials of what follows, in order — so it is derivable from the
  remainder and cannot stand for another journal — and what is left must equal
  a name the record itself carries outright.
- **Every PubMed citation a DOI resolves to is fetched, not one of them.**
  Where `esummary` attributed two PMIDs to one DOI, only the number that sorted
  last was fetched, and its `PT` decided the entry's retraction status — so a
  work PubMed records as retracted under one citation and not the other
  reported clean. Both are now fetched, and the post-publication status of
  either is the entry's: the `PT` retraction flag and MEDLINE's `ECI`
  cross-reference, which is filed on the citation a concern was raised against
  and so was previously reported or lost according to `efetch`'s ordering. The
  title and byline still come from the citation that arrived first, and
  `Record.pmid` is still withheld: two citations of one work agree on those,
  and a record listing another work's identifier as its own must not be able to
  supply an entry's metadata by being the retracted one.
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
