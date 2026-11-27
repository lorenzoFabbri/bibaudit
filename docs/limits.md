# Limits

This tool claims that its findings can be checked, so the boundary of what it establishes is part of what it reports. The [home page](index.md) summarises that boundary; this page states it in full. Read it before treating a passing run as assurance.

## Metadata, not argument

Every report carries the same sentence, `report.LIMITS_NOTICE`:

> bibaudit verifies that each reference exists and that its stored metadata matches the publisher's record. It does not and cannot verify that a cited work supports the statement it is attached to — that requires reading the paper.

The terminal report prints it dimmed, below the closing banner, on every run, failing or passing. The JSON report carries the identical string as the payload's `limits` field, so a dashboard built on `--format json` cannot show the counts without also having been handed the caveat.

bibaudit can tell you that the DOI stored beside a reference resolves to a different paper than the one the entry describes: that is `WRONG-WORK`, a failure that a plain "does the DOI resolve" check passes. It cannot tell you whether the paper that DOI *does* resolve to supports the sentence you attached the citation to. The comparison works on titles, author lists, years, container titles, volume, issue, first page, publisher and the PMID, and none of those is evidence about a claim in your manuscript. A correctly recorded citation of an irrelevant paper passes every check in this tool, and nothing here will say otherwise. Reading the paper is the only remedy.

A resolving identifier is the weakest evidence on that list, and a PMID needs saying so about because it looks like more than it is. A PMID that resolves establishes one fact: NLM has indexed some work under that number. It does not establish that the work is the one the entry describes. The field comparison establishes that, which is why a PMID lookup is followed by the same title, author and year checks a DOI lookup gets. It does not establish that the work is sound: PubMed indexes journals and does not judge findings, and a retracted paper keeps its PMID. PMID 9500320, the Wakefield paper, resolves exactly as it did before its retraction, which is a field on that record and not its removal. And a PMID says nothing about whether that work supports the sentence it was cited for. Two identifiers agreeing with each other is a stronger statement than one resolving, and it is still a statement about identifiers.

## It cannot prove that a work does not exist

`BAD-ID` is worded as narrowly as the evidence allows: *resolves in no consulted registry*. That is a fact about the registries that answered on that run, not about the world. A registry that could not be reached leaves the reference `UNCHECKED` instead, because confusing ignorance with absence is the one way this tool could accuse a real paper of not existing.

How many registries "no consulted registry" covers depends on the identifier. A DOI is put to Crossref and then to DataCite. A PMID is put to PubMed alone, and an ISBN to Open Library alone.

For a PMID, no second registry would add independent evidence. Other indexes do resolve PMIDs: Europe PMC, which this tool already queries for the identifier-less search, answers for 9500320 with the Wakefield paper. But it serves that record from MEDLINE, and a `SRC:MED` hit is NLM's own citation redistributed. Elsewhere in this tool Europe PMC corroborates a title/author match because there it is curated separately, indexing preprints and grey literature Crossref never saw; on a PMID it has nothing separate to say, and agreement between the two would be one answer read twice. So a `BAD-ID` on a PMID rests on one index's response, and no independent second index exists to consult. The ISBN case is thinner still; see below.

Registry coverage has real gaps: pre-1990 work, grey literature, non-English publishing, and books. Books are the widest of them. Most were never issued a DOI, so an entry carrying an `isbn` and no DOI is resolved through Open Library instead, whose catalogue is crowd-sourced and noticeably thinner than Crossref's: many records carry a title and nothing else, no authors and no publication year. That is why `compare.confirm_without_id` refuses outright any candidate carrying neither an author list nor a year, however well its title scores. A title-only record is evidence that *some* book with that title exists, not that it is the right one, and accepting it would be the plausible but wrong match that function exists to prevent.

An entry that carries no identifier and that nothing can confirm to that standard is reported `UNCONFIRMED`, printed under the heading "no identifier and no confident registry match — needs review", with an issue note saying why no candidate was accepted. It is in the failing set, because a fabricated reference takes that shape and a person needs to look at it. It means *needs review*, never *fabricated*, and the report never uses that word.

## What `PASS` means, and what it does not

!!! warning "`PASS` is the run banner, not a verdict"

    The run's closing banner is `PASS — no reference in the failing set` or a `FAIL — …` line naming what failed. It reads `FAIL` when any reference's verdict falls in the failing set, by default `RETRACTED`, `BAD-ID`, `WRONG-WORK`, `FIELD-MISMATCH` and `UNCONFIRMED`, and also when a bibliography entry could not be read and so was not audited, or a `[@citekey]` resolves to no bibliography entry. The exit code follows the same test, 0 or 1. A tool that could not run at all (bad arguments, an unreadable input, a broken config) exits 2.

    `DISPUTED`, `INCOMPLETE`, `ADJUDICATED`, `REGISTRY-ARTIFACT`, `TITLE-DRIFT`, `COSMETIC`, `UNCHECKED` and `OK` are all outside that set, so a run in which no registry could be reached reports every reference `UNCHECKED`, prints `PASS`, and exits 0. That is deliberate: an outage is not a defect in your bibliography, and a check that breaks the build whenever Crossref is down for an afternoon is a check people learn to bypass. An `--offline` run against a cache that is empty or past its lifetime produces the same result, for the same reason.

    Read the summary counts, not the banner. `bibaudit — N references checked` and the per-verdict tally above the banner say how much was established.

`OK`, the per-reference verdict, is the stronger word, and it is still narrower than "correct": it means every field bibaudit compared agreed with a registry that answered. The result itself records which registries those were. `consulted` distinguishes `answered`, `unreachable` and `not-asked` per registry, so an `OK` reached while PubMed was unreachable can be told apart from one reached with PubMed's agreement. `--no-corroborate` does not show there: it drops PubMed as a corroborating source for the field comparison, but the retraction check still queries PubMed, so `consulted` records it as `answered` unless `--no-retraction-check` was passed as well. [Verdicts](verdicts.md) covers all thirteen and which of them break a build.

## What a clean retraction result establishes

A clean retraction result establishes less than the absence of a `RETRACTED` line suggests. [Retraction](retraction.md) describes the sources and how they are combined; this section states where each of them stops.

Two of the four sources are keyed on a DOI. Crossref's `updated-by` and Retraction Watch's export are looked up by DOI, either the one stored in the entry or one a title/author search confirmed. An entry resolved by another identifier loses those two, and what it keeps depends on the identifier. A reference resolved by its **PMID** keeps both of PubMed's sources, NLM's `PT - Retracted Publication` and the `ECI` cross-reference, because both are fields of the MEDLINE record the lookup already returned. A concern NLM recorded is therefore reported, and what goes unreported is a retraction that only Retraction Watch or only a publisher's Crossref deposit knows about. A book resolved through its **ISBN** alone is not checked for retraction in any way: Open Library mints no DOI to ask the first two about, and holds no MEDLINE record to read the other two off. Neither case passes in silence. Both raise `status/not-asked`, which names the retraction sources the run did not ask and prints beside the banner exactly as an outage does.

Crossref's and PubMed's own flags depend on somebody having recorded the linkage, through a publisher deposit or NLM's curation. A retraction nobody deposited and nobody indexed is invisible to them.

Retraction Watch's export is a snapshot, and can be up to seven days old when a run reads it. It carries what its curators have recorded and nothing else, and the parsed index is cached and refetched only once it is seven days old, so a retraction logged there in the last few days may not yet be reflected in a run.

A source that went unheard is stated. When a registry that carries the signal was unreachable or was never asked, and no registry that did answer records a retraction, the run prints `retraction status not corroborated for N reference(s)` beside the banner, naming the sources and which of the two happened to them. Unreachable and unasked sources get separate lines, because a rerun may settle an outage and no rerun asks a DOI-keyed source about a reference that has no DOI. The unasked line also says which of *its* two reasons applied, a source that takes an identifier the reference does not carry or one that had a key and was not queried on this run, because only the second has a remedy.

Five sources are deliberately excluded from both lines, because bibaudit reads no retraction signal from any of them, and naming one would put a manufactured doubt on a whole class of entry. DataCite and Open Library have no retraction, withdrawal or concern element in their data models at all. For Europe PMC and OpenAlex the limit is bibaudit's own, which is why it is stated on this page: **Europe PMC does publish retraction linkage**, in `commentCorrectionList`, and `registries/search.py` reads neither that nor anything like it. Both are consulted for candidates only, on entries carrying no identifier. zbMATH Open is asked for a byline and nothing else. A reference that *no* registry answered for is `UNCHECKED`, and its verdict states that gap, not this line.

`--no-retraction-check` turns the independent pair off: Retraction Watch's export and PubMed's `ECI`. A Crossref or PubMed record that itself carries a retraction linkage still fails, and every reference records `retraction-watch` as `not-asked` and carries the gap. The run cannot state the `ECI` half: PubMed answered for the citation, so `consulted` says `answered` and no `not-asked` line names it. [Retraction](retraction.md) says why there is no fourth consultation state for it.

## A person recorded by the name they go by

Forenames are compared by their first initial, so an entry holding `Edgerton, V R` and a registry holding `Edgerton, Reggie` disagree, though they are one person, *V. Reggie Edgerton*, recorded by his middle name on the deposit and by his initials in MEDLINE. PMID 990943 is the live instance, and it was the only false alarm the check produced over 3,963 MEDLINE/Crossref pairs of the same work.

Letting a side written entirely as initials answer with any of them, so that `Reggie` may match the `R` in `V R`, would remove that false alarm. Measured on the same sample, it buys back the one false alarm and costs **11** caught forename substitutions. Eleven misses per false alarm avoided is the wrong trade for the check whose miss credits the wrong person, so bibaudit keeps the narrow rule and this stays a stated limit. A run that hits it prints both forenames in full; `kind = "forename"` in a project's `.bibaudit.toml` adjudicates it.

## One registry romanises a byline and the other does not

MEDLINE writes an author's surname in ASCII; Crossref deposits it as the author writes it. `fold` closes most of that gap by romanising the letters NFKD leaves whole — `Kjær` and `Kjaer`, `Weiß` and `Weiss`, `Guðmundsdóttir` and `Gudmundsdottir` (PMID 38747246, `10.1093/eurheartj/ehae331`, where the two registries spell one person both ways) — and each of those letters has exactly **one** ASCII spelling in the table.

NLM does not always pick the same one. Eth is `d` on the overwhelming majority of its bylines and `eth` on a few: `Gudmundsdottir[au]` answers 884 records and `Guethmundsdottir[au]` none, `Sigurdsson[au]` 2,433 against `Sigurethsson[au]` 5, `Fridriksdottir[au]` 114 against `Friethriksdottir[au]` 2. On the minority records the two spellings are two comparison keys — `Friðriksdóttir` against MEDLINE's `Friethriksdottir`, PMID 42541912 — and a correct entry reports `authors/mismatch`.

This stays a stated limit, and eth gets no second row in the table. A letter with two mappings produces two keys, and nothing in the pair being compared says which of them the other side used, so picking one silently reintroduces the failure on the other spelling. A run that hits it prints both surnames in full, which is what the report is for.

## A letter drawn from another script, inside a Latin word

A publisher sometimes deposits a name with one letter taken from Cyrillic or Greek where the Latin lookalike belongs — the forename of creator four in `10.26442/00403660.2024.07.202907` opens on CYRILLIC CAPITAL LETTER TE, not on `T`, and NLM's own XML for the same paper (PMID 39106512) carries the same code point. `fold` reads such a letter as the Latin letter it is drawn as, using Unicode's own confusables table, so the name compares as the name.

It does that only where the value carries a Latin letter of its own **and** every non-Latin letter in it is one the table has a Latin twin for. Both halves matter: without the first, a word written in Cyrillic would come back as a confident Latin key instead of an empty one; without the second, a name a registry deposits in its own script would be rewritten letter by letter rather than reported as one this tool cannot express.

23 of the 91,226 creators in a 32,000-work random Crossref sample carry a mixed-script name, and 19 of them are repaired. The other four are left alone on purpose: two surnames spelled with a lower-case Greek letter, which has no twin here and cannot get one without taking `TNF-α` and `IFN-γ` with it; a Ukrainian surname using a letter no Latin one is drawn as; and a Russian name written in Cyrillic that happens to carry one stray Latin letter, where leaving it unreadable is the point.

The remaining limit is a value mixing Latin with a short word spelled *only* in letters that have Latin twins — 9 of Russian's 33 do — which is repaired as though it were damage. No instance has been seen in any sample taken here, and a run that hits one prints both values in full.

## Registries are sometimes wrong

The registry is a witness, not an authority, and bibaudit has no standing to decide that the registry is right and your bibliography wrong. That is why it reports and never rewrites, and it is a limit as much as a principle: a finding tells you two records disagree, not which of them to believe.

Where a difference is a known, reproducible registry defect (mojibake surnames, an article number recorded against a page range, a title the registry stores truncated where the entry has it in full), it is suppressed as `REGISTRY-ARTIFACT` and still counted in the summary, with the case written up in [registry defects](registry-artifacts.md) so you can challenge the call. Where two registries disagree with each other and not with you, the verdict is `DISPUTED`, both values are printed, and neither is preferred: the tool has no basis for choosing between two curated sources, and choosing anyway would be the same overreach as rewriting the file. `DISPUTED` does not fail a build; it is handed to you to settle. Where you have settled one, record it in `.bibaudit.toml` with a reason (see [adjudicating a difference](suppressions.md)), and the entry reports `ADJUDICATED`, counted apart from a documented defect because one project's decision and a settled registry bug are different amounts of assurance.
