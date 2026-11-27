---
name: registry-fixtures
description: How to add, describe and verify a file in tests/data/ — the PROVENANCE.toml entry each one owes, what `pytest -m network` establishes that the offline suite cannot, and the fields the networked comparison ignores. Load when adding a fixture, re-fetching a drifted one, or widening the ignored-field list.
---

# Fixtures in tests/data/

## Adding a fixture

The fixtures in `tests/data/` are real registry responses, including the defective ones. Do not "clean them up": the mojibake and the doubled MathML are what the tests exercise.

1. Fetch the whole response and commit it unedited: all of its fields, not only the ones your test reads. A value dropped quietly and a value invented look the same from outside, which is why there is no way to declare a fixture partial.
2. Add a `[[fixture]]` entry to `tests/data/PROVENANCE.toml` naming the file, the registry, the identifier you requested it under, that request's URL, and the date. The header of that file defines every field. A fixture with no entry fails the build, so this is not a step you can forget; an entry naming a URL no client in `src/bibaudit/registries/` would issue fails too.
3. Run `uv run pytest -m network`. It is the only step that verifies anything.

An entry is a claim by whoever wrote it. The offline suite establishes only that the claim hangs together (that the file is a MEDLINE citation, that the PMID in it is the PMID you wrote down), and a citation invented around a live PMID satisfies all of that. `pytest -m network` re-fetches each URL and compares the answer with the bytes on disk, and nothing else in this repository can tell a recorded response from a written one. It is deselected by default so the ordinary suite stays offline, which means a fixture nobody has re-fetched is a fixture nobody has verified.

## What the networked comparison ignores

MEDLINE and the Retraction Watch rows are compared with nothing ignored. Three registries stamp their own record-keeping into the payload, and comparing on those would redden the check every week over values no deposit carries, until nobody ran it:

- Crossref: `indexed` and `deposited`, the index and last-deposit timestamps; `is-referenced-by-count`, a citation counter; and `link`, whose URL follows the publisher's hosting rather than the deposit.
- DataCite: `updated`, and the aggregates `viewCount`, `downloadCount`, `citationCount`, `citationsOverTime`, `referenceCount`, `partCount`, `partOfCount`, `versionCount` and `versionOfCount`. One level over, under `data.relationships`, the same goes for `citations`, the list of citing records that `citationCount` counts, because comparing the list while ignoring the count would leave the check red for the same reason. Every other relationship is compared.
- OpenAlex: `db_response_time_ms` under `meta`, the time OpenAlex took to answer this request; and `relevance_score` on each result, its search engine's score, which moves with the index rather than the work; and `fwci` and `citation_normalized_percentile` on each result, citation metrics OpenAlex recomputes as citing works are indexed. The results are compared in order, so a re-ranking is reported.

Open Library edition records are compared with nothing ignored.

Everything else is compared, and a difference means the record was revised — re-fetch, read the diff, commit it — or the file was never that record. Widening this list hides a fabricated value, so it grows only for a field that is demonstrably the registry's own bookkeeping, and each addition is named here: `tests/test_fixture_provenance.py` fails the build for one that is not.

## If a fixture is not a registry response

A bibliography the tool *reads* — a `.bib`, a Quarto page, a Zotero export — still needs an entry, with `source = "none"` and a `note` saying what it is. Nothing re-fetches those, and nothing checks the identifiers inside them: their DOIs are the thing under audit and some are wrong deliberately. The note is the only account such a file will ever have.

So the tests demand one thing of the note, and only for a file that parses as a JSON list of objects: it must name every `id` or `key` in the file. A note nothing reads is a note nothing corrects.
