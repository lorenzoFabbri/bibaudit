# Command line

There are two subcommands. `check` audits references; `cache` inspects or
empties the store `check` reads and writes. `bibaudit --version` prints the
installed version and exits.

```bash
bibaudit check references.bib
bibaudit check notes/**/*.md --bibliography library.bib
bibaudit check ~/Zotero/zotero.sqlite
bibaudit check . --format json --output audit.json
```

`check` takes one or more `paths`: bibliographies (`.bib`), documents
(`.qmd`/`.md`), Zotero libraries (`zotero.sqlite`, a CSL-JSON export, or the
word `local` for a running Zotero), or directories containing them. A directory
is walked recursively, except that a directory holding a `zotero.sqlite` is read
as a library instead; dot-prefixed components, `_site`, `_freeze`,
`node_modules` and any `*.suggested.bib` are skipped during that walk. Naming a
`.suggested.bib` file on the command line still audits it — that is exactly what
reviewing a proposal before applying it requires. The paths matched against
those rules are relative to the directory being scanned, so a project that
lives under a dot-directory is not silently emptied of every file.

| Flag | Default | |
|---|---|---|
| `-b`, `--bibliography PATH` | none | bibliography used to resolve `[@citekey]` references in documents; repeatable. Discovered from Quarto/YAML front matter when omitted |
| `--format {text,json}` | `text` | report format |
| `-o`, `--output PATH` | stdout | write the report here instead of stdout |
| `--fail-on LIST` | `BAD-ID,FIELD-MISMATCH,RETRACTED,UNCONFIRMED,WRONG-WORK` | comma-separated verdicts that make the exit code non-zero. Pass an empty string to never fail |

`--fail-on` is split on commas, stripped and upper-cased; it is not validated
against the known verdicts, so a misspelled name quietly fails nothing. Whatever
it resolves to governs the exit code, the closing `PASS`/`FAIL` banner, and the
`summary.failing_verdicts` and `summary.exit_code` fields of the JSON report —
one policy, stated in every place the reader might read it from. See
[verdicts](verdicts.md) for what each name means and [in CI](ci.md) for the exit
codes. One thing it cannot switch off: a `[@citekey]` used in a document with no
matching bibliography entry exits `1` on its own, whatever `--fail-on` says and
whichever format was asked for.

## Registries

| Flag | Default | |
|---|---|---|
| `--mailto EMAIL` | none | contact address sent to Crossref and NCBI; puts requests in Crossref's polite pool. No account or key is needed |
| `--no-corroborate` | off | skip PubMed corroboration |
| `--no-search` | off | do not try to confirm entries that carry no identifier |
| `--no-retraction-check` | off | skip the independent Retraction Watch / PubMed expression-of-concern check |
| `--no-isbn` | off | do not resolve books through Open Library |
| `--no-europepmc` | off | drop Europe PMC from the identifier-less search |
| `--no-openalex` | off | drop OpenAlex from the identifier-less search |
| `--offline` | off | use only cached registry responses |
| `--refresh` | off | ignore cached responses and refetch |
| `--cache-dir PATH` | per-user cache directory | where registry responses are cached |
| `--cache-ttl DAYS` | `90` | how long a cached registry response stays valid |
| `--timeout SECONDS` | `30.0` | per-request timeout |

`--mailto` is appended to the User-Agent that every request carries, in the form
`bibaudit/<version> (+https://github.com/lorenzoFabbri/bibaudit); mailto=you@example.org`.
Crossref grants its polite pool — lower latency, priority during load-shedding —
to requests carrying a contact address in exactly that form, so omitting it is a
silent slowdown rather than an error, which is how it stays wrong for a long
time. It is not an API key, and there is no API key anywhere in this tool:
nothing bibaudit sends to any registry is a credential, and no service it
consults requires an account.

**`--no-corroborate`** removes PubMed from the DOI resolution path. What you
lose is the independently curated second opinion — the one that catches a defect
in Crossref's own deposit rather than agreeing with it. What you do not lose on
an otherwise default run is the requests: the retraction check has its own
PubMed leg, handed the identical DOI list, so the same E-utilities queries are
issued either way — by the corroboration pass, or, once this flag removes it, by
the retraction pass that had been reading them back out of the cache the
corroboration pass filled. `--help` says the flag halves request volume; that
holds only alongside `--no-retraction-check`, which is also what it takes to
stop querying PubMed at all.

**`--no-search`** stops entries carrying no DOI, PMID, arXiv ID or ISBN from
being looked up by title and author at all. It does not quieten them — in a run
where every registry answered, such an entry is reported `UNCONFIRMED`, a
failing verdict, having consulted nothing. The flag stops the attempt to clear
them, not the consequence of their not being cleared.

**`--no-retraction-check`** turns off the independent pair: Retraction Watch's
own export and PubMed's expression-of-concern cross-reference. It does not turn
off retraction reporting. A retraction the publisher deposited in Crossref's
`updated-by` linkage, or that MEDLINE flags with `PT  - Retracted Publication`,
is read off the record itself and still fails as `RETRACTED`. What goes unnoticed
is a retraction that was never deposited in either place — the gap the
independent check exists for. No finding marks the narrower coverage: the
`status/retraction-unverified` note fires for a source that could not be
*reached*, not for one that was never asked. The only trace is in the JSON
report, whose `consulted` map stops naming `retraction-watch` at all.
[Retraction](retraction.md) covers the four sources in full.

**`--no-isbn`** means Open Library is never constructed, so no book is resolved
through it — neither by ISBN nor by the title/author search used for books with
no identifier at all. A book with no identifier then loses the one candidate
source organised around books. A book that stores only an ISBN loses more than
that: it carries an identifier that nothing was asked about, and in a run where
no registry was unreachable it is reported `BAD-ID`, a failing verdict.

!!! note "The flag's own help text overstates this"

    `--help` says an ISBN-only book "falls back to UNCONFIRMED". The code path
    reaches `BAD-ID` instead, because the ISBN is a stored identifier and no
    record resolved it. The behaviour above is what the tool does.

**`--no-europepmc`** and **`--no-openalex`** affect only the confirmation path
for entries with no identifier; neither is consulted for a DOI. They are not
symmetrical. Europe PMC is curated separately from Crossref and indexes material
Crossref does not — preprints, some grey literature, agency reports — so
dropping it loses candidates nothing else in the tool would find. OpenAlex
substantially re-crawls Crossref's deposits, so it never corroborates a match on
its own and is consulted for discovery only; dropping it narrows what can be
*found*, never what would have been *accepted*. Every candidate, whichever
source produced it, is judged by the identical bar. When *every* enabled search
source fails for an entry, the search reports ignorance rather than an empty
result, so a total outage leaves that entry `UNCHECKED` rather than
`UNCONFIRMED`.

**`--offline`** never opens a socket. A cached answer is replayed — including a
cached 404, which is a fact about a work and as worth replaying as a successful
response — and anything uncached is reported `UNCHECKED` rather than fetched. A
cached response older than `--cache-ttl` is a miss like any other, so the same
command re-run outside that window derives an honestly narrower report, not a
different one. `--refresh` cannot be honoured without a network: given both, the
cache is still read.

`--cache-dir` defaults to `$XDG_CACHE_HOME/bibaudit` when that variable is set on
any platform, otherwise `~/Library/Caches/bibaudit` on macOS and
`~/.cache/bibaudit` elsewhere. It relocates the per-request registry cache only:
the parsed Retraction Watch index is kept separately, in a `retraction-watch/`
subdirectory of the *default* cache directory, on its own seven-day lifetime
that `--cache-ttl` does not change. Ninety days is right for bibliographic
fields, which do not change under a fixed DOI; seven is right for a status,
which does.

`--timeout` bounds one request. A logical lookup is retried — one attempt plus
four retries, backing off 1, 2, 4 and 8 seconds, or by whatever a `Retry-After`
header asks for up to a minute — after which the registry counts as unreachable,
which is ignorance (`UNCHECKED`), never a missing work (`BAD-ID`).

## Report detail

| Flag | Default | |
|---|---|---|
| `-v`, `--verbose` | off | include informational verdicts (cosmetic, drift, registry artifacts) |
| `--show-suppressed` | off | list differences that were suppressed and why |
| `--no-citekey-check` | off | skip checking that every `[@citekey]` resolves to a bibliography entry |
| `--suggest` | off | write `<name>.suggested.bib` and `<name>.suggested.diff` beside each checked `.bib` with a fillable gap |

By default the text report prints only the verdict groups a reader must act on —
the failing set plus `DISPUTED`, where two curated registries contradict each
other and only a person can settle it — and within those, only issues above
informational severity. `--verbose` prints every group and every issue.

`--show-suppressed` lists what was suppressed and why, beneath the entry it
belongs to, and works without `--verbose`: it pulls in the entries that actually
carry a suppressed difference even though their verdict group is not printed by
default. The counts appear in the summary either way, because a report that
quietly drops what it decided not to tell you is not an audit. See
[adjudicating a difference](suppressions.md).

`--no-citekey-check` drops both the unresolved-key list and the exit `1` it
causes. The check runs only when documents were scanned *and* at least one
bibliography entry was read, so it is already inert on a bare `.bib` run.

`--suggest` fills only fields an entry has none of, writes `<name>.suggested.bib`
and `<name>.suggested.diff` beside the source, and never opens the source for
writing. Its notices go to stderr, not to the report stream, so a `--format json`
stdout stays parseable; it is silent when there was nothing to propose. See
[proposing fixes](suggest.md).

## Thresholds

| Flag | Default | |
|---|---|---|
| `--title-wrong-work R` | `0.55` | title similarity below which the identifier may point at a different work |
| `--title-mismatch R` | `0.85` | title similarity below which titles are reported as disagreeing |

Both are ratios on the same similarity function. `--title-wrong-work` can sit as
low as it does only because a title scoring below it is cross-checked against the
author list before `WRONG-WORK` is declared: one fuzzy score is not enough
evidence to accuse a bibliography of citing the wrong paper. Lowering
`--title-mismatch` quietens disagreements at the cost of missing real ones;
raising it does the reverse. The relaxed bands used for books and chapters, the
band above which a title difference drops to informational severity, and the bar
for confirming an entry with no identifier are not exposed on the command line.

## `bibaudit cache`

```bash
bibaudit cache info
bibaudit cache clear
```

`info` prints the cache directory, how many responses are stored and their total
size; `clear` empties it. Both accept `--cache-dir`, which has to match the one
`check` was given. If the directory cannot be created, both say so on stderr
rather than reporting a working, empty cache — an unusable cache and an empty one
otherwise produce identical output, and the difference is exactly what someone
diagnosing why `--offline` reports everything `UNCHECKED` needs to see.
