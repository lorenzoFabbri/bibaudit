"""Where each file in ``tests/data`` came from, and what says so.

Every fixture in this project is described, in the ``registry-fixtures``
skill, as a real registry response. Ten were not. They were
found one at a time, always by somebody opening a file for an unrelated
reason, because a claim made in prose is enforced by nobody: no test
re-fetched a fixture, none enumerated the directory, and the one test
carrying the ``network`` marker asserts on the marker expression and reaches
no registry at all.

``tests/data/PROVENANCE.toml`` is that claim written down per file, and this
module is what holds it to something.

**Offline** — every check in :data:`CHECKS`, run over the real manifest on
every ordinary test run. They establish that the manifest is *coherent*: that
no file is undeclared and no entry is orphaned, that a file's own content
agrees with the ``source`` and ``id`` claimed for it, and that ``url`` is the
request ``src/bibaudit/registries/`` would issue for that identifier rather
than an address somebody typed. None of that can tell a recorded response
from an invented one. A MEDLINE citation written around a real PMID satisfies
all of it, and four of the ten were exactly that: PMIDs 27532363, 15455608,
33069326 and 7912306 all resolve, to four papers none of those files was
about.

**Networked** — ``uv run pytest -m network``, opt-in and deselected by
default. Each fixture's ``url`` is fetched again and the answer compared with
the bytes on disk. This is the half that establishes anything: it is the only
thing in this repository that can distinguish a registry's answer from a
plausible imitation of one, and it is nobody's default, so a fixture nobody
has re-fetched is a fixture nobody has verified.

The split is deliberate and is the whole design. The offline suite must pass
with no connection — CLAUDE.md's rule, and the reason the network marker
exists — so the check that runs everywhere cannot be a fetch. What runs
everywhere is the part that makes *silence impossible*: a fixture added
without an entry fails the build, so the cost of the network check is a
command somebody has to run, never a file nobody knew to look at.
"""

from __future__ import annotations

import csv
import io
import json
import re
import tomllib
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import pytest

from bibaudit.normalize import normalize_doi
from bibaudit.registries import crossref, datacite, pubmed, retractions
from bibaudit.registries.http import Client

DATA = Path(__file__).parent / "data"
MANIFEST = DATA / "PROVENANCE.toml"
RULES = (
    Path(__file__).resolve().parents[1] / ".claude" / "skills" / "registry-fixtures" / "SKILL.md"
)

#: Everything a ``[[fixture]]`` table may carry. Anything else is a typo, and
#: a typo here reads as a requirement met rather than as an error — the same
#: reason ``suppress._ALLOWED_KEYS`` exists one directory over.
_ALLOWED_KEYS = frozenset({"file", "source", "id", "url", "fetched", "holds", "synthetic", "note"})

#: ``source`` values. ``"none"`` is a bibliography the tool *reads*: no
#: registry answers for it, so nothing can re-fetch it.
_SOURCES = frozenset({"pubmed", "crossref", "datacite", "retraction-watch", "none"})

_REGISTRIES = _SOURCES - {"none"}

#: NLM writes the PMID once per citation, on its own line, as the first field.
_PMID_LINE = re.compile(r"^PMID- (\d+)\s*$", re.MULTILINE)

#: The first three columns of Retraction Watch's export, which is enough to
#: recognise it and short enough not to break when RW appends a column.
_RW_HEADER = "Record ID,Title,Subject,"

# ---------------------------------------------------------------------------
# Fields the networked comparison ignores, and why each one is here.
#
# This is a suppression list. The ``registry-fixtures`` skill names every entry
# and ``test_every_ignored_field_is_written_up_for_a_contributor`` fails the build
# for one it does not: a difference the gate declines to report is exactly the
# kind of thing a reader has to be able to look up and argue with.
#
# The list is short on purpose. Too wide and a fabricated value hides inside
# it; too narrow and the check reddens over a counter nobody typed, gets
# ignored, and protects nothing. Everything here is either a timestamp the
# registry stamps on its own record-keeping or an aggregate it recomputes —
# nothing a deposit carries about the work itself.
# ---------------------------------------------------------------------------

_VOLATILE_CROSSREF = frozenset(
    {
        # Crossref's own index and the publisher's last deposit, both moving
        # without the work changing: witnessed on 10.1016/s0140-6736(97)11096-0
        # and 10.1097/00008469-199710000-00007 within nine days of capture.
        "indexed",
        "deposited",
        # A citation counter. Same two records, 1995 -> 1996 over the same
        # nine days.
        "is-referenced-by-count",
        # Full-text and similarity-checking targets, which follow the
        # publisher's hosting rather than the deposit: the Cancer Prev journal
        # link moved from journals.lww.com to www.ovid.com over those nine days
        # with every bibliographic field unchanged.
        "link",
    }
)

_VOLATILE_DATACITE = frozenset(
    {
        # DataCite's own last-modified stamp, moving on records whose every
        # other attribute is identical: witnessed on 10.26268/heal.uoi.19814
        # and 10.25431/11380_1192850.
        "updated",
        # Aggregates DataCite recomputes from other people's deposits and from
        # its own traffic. Listed for what they are rather than for having been
        # seen to move: 10.48550/arxiv.1706.03762 is the Transformers preprint,
        # and its citationCount is not a fact about the record's content.
        "viewCount",
        "downloadCount",
        "citationCount",
        # The same counter broken down by year, and it moves for the same
        # reason: the Transformers preprint's 2026 bucket grew between two
        # runs of this check nine days apart while its every deposited
        # attribute stayed put.
        "citationsOverTime",
        "referenceCount",
        "partCount",
        "partOfCount",
        "versionCount",
        "versionOfCount",
    }
)

#: The same aggregates one level over, under ``data.relationships``, where
#: DataCite lists the records behind a count rather than the count. Ignoring
#: ``citationCount`` and comparing the list it counts leaves the check red for
#: the identical reason: the Transformers preprint went from 55 citing DOIs to
#: 57 between two runs nine days apart, with every deposited attribute
#: unchanged. Only the relationship witnessed moving is here; the rest are
#: compared.
_VOLATILE_DATACITE_RELATIONSHIPS = frozenset({"citations"})


def _entries() -> list[dict[str, Any]]:
    """Every ``[[fixture]]`` table, in file order."""
    with MANIFEST.open("rb") as handle:
        loaded: list[dict[str, Any]] = tomllib.load(handle)["fixture"]
    return loaded


ENTRIES = _entries()


def fixture_files() -> list[str]:
    """Every fixture in ``tests/data``, as a path relative to it.

    Recursive, so that a subdirectory of files is covered rather than counted
    as one undeclared directory — a fixture is no less a fixture for being
    filed one level down.

    Dot-prefixed names are skipped: ``.DS_Store`` is in this project's
    ``.gitignore`` for the obvious reason, nothing here loads a fixture by a
    dotted name, and a check that reddens because somebody opened the folder
    in Finder is one that gets switched off.
    """
    return sorted(
        relative.as_posix()
        for path in DATA.rglob("*")
        if path.is_file()
        and (relative := path.relative_to(DATA)) != Path(MANIFEST.name)
        and not any(part.startswith(".") for part in relative.parts)
    )


def _of_source(source: str) -> list[dict[str, Any]]:
    return [entry for entry in ENTRIES if entry.get("source") == source]


def _ids(entries: Sequence[dict[str, Any]]) -> list[str]:
    return [str(entry.get("file")) for entry in entries]


# ---------------------------------------------------------------------------
# Reading a fixture's own account of itself.
#
# Each reader returns the identifier the *file* carries, or None if the file
# is not that kind of response at all. Together they are how a manifest entry
# is checked against the thing it describes, in both directions: an entry
# claiming the wrong PMID fails, and so does a MEDLINE record filed under
# ``source = "none"`` where nothing would ever re-fetch it.
# ---------------------------------------------------------------------------


def medline_pmid(text: str) -> str | None:
    """The PMID of a MEDLINE ``efetch`` response, or None if *text* is not one."""
    found = _PMID_LINE.search(text)
    return found.group(1) if found else None


def crossref_doi(payload: object) -> tuple[str, str] | None:
    """``(doi, holds)`` for a Crossref response or a bare work, else None.

    ``holds`` distinguishes the two depths a Crossref answer is recorded at:
    ``"response"`` for the whole body, ``"work"`` for the ``message`` object
    alone — which is what ``/works?filter=doi:...`` yields per item and what
    ``Crossref.by_dois`` passes on, so both are things the client really sees.
    """
    if not isinstance(payload, dict):
        return None
    message = payload.get("message")
    if isinstance(message, dict) and isinstance(doi := message.get("DOI"), str):
        return doi, "response"
    if isinstance(doi := payload.get("DOI"), str):
        return doi, "work"
    return None


def datacite_doi(payload: object) -> str | None:
    """The DOI of a DataCite ``/dois/`` response, or None."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    attributes = data.get("attributes") if isinstance(data, dict) else None
    if isinstance(attributes, dict) and isinstance(doi := attributes.get("doi"), str):
        return doi
    return None


def described_by(path: Path) -> tuple[str, str | None, str | None]:
    """``(source, identifier, holds)`` as the file itself shows them.

    ``("none", None, None)`` for anything that is not a registry answer. The
    point is not classification for its own sake: it is that the manifest
    cannot file a MEDLINE citation as something no network test will ever
    re-fetch.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    if (pmid := medline_pmid(text)) is not None:
        return "pubmed", pmid, None
    if text.startswith(_RW_HEADER):
        return "retraction-watch", None, None
    try:
        payload = json.loads(text)
    except ValueError:
        return "none", None, None
    if (doi := datacite_doi(payload)) is not None:
        return "datacite", doi, None
    if (found := crossref_doi(payload)) is not None:
        return "crossref", found[0], found[1]
    return "none", None, None


def expected_url(source: str, identifier: str | None) -> str:
    """The request ``src/bibaudit/registries/`` issues for *identifier*.

    Built from the registry modules' own constants and quoting rules rather
    than restated here, so a manifest URL is checked against the code that
    would really be run and not against a second copy of it that can drift.
    """
    if source == "pubmed":
        params = urlencode(
            {"db": "pubmed", "id": identifier or "", "rettype": "medline", "retmode": "text"}
        )
        return f"{pubmed._EFETCH_URL}?{params}"
    if source == "crossref":
        return f"{crossref._API_ROOT}/works/{quote(identifier or '', safe='')}"
    if source == "datacite":
        return f"{datacite._DOIS_URL}{quote(identifier or '', safe='/')}"
    return retractions._RW_CSV_URL


def same_identifier(source: str, claimed: str, found: str) -> bool:
    """Whether two identifiers name the same record, per *source*'s own rules."""
    if source == "pubmed":
        return claimed == found
    return normalize_doi(claimed) == normalize_doi(found)


# ---------------------------------------------------------------------------
# The offline checks.
#
# Each takes the manifest and returns the complaints it has, so the same
# function can be run over the real file (which must be clean) and over a
# doctored copy (which must not be). A check asserted inline in a test proves
# only that today's manifest passes it; it never proves the check would fail
# anything.
# ---------------------------------------------------------------------------


def check_every_file_is_declared(entries: Sequence[dict[str, Any]]) -> list[str]:
    """Neither an undeclared fixture nor an entry with no fixture.

    Both directions, because each is a way for the manifest to stop describing
    the directory: the first is a file added without an entry — the failure
    this whole mechanism exists for — and the second is an entry left behind
    by a deletion, which quietly makes the count look right.
    """
    on_disk = set(fixture_files())
    declared = {str(entry.get("file")) for entry in entries}
    return [
        f"{name}: in tests/data/ and not in the manifest" for name in sorted(on_disk - declared)
    ] + [f"{name}: in the manifest and not in tests/data/" for name in sorted(declared - on_disk)]


def check_no_file_is_claimed_twice(entries: Sequence[dict[str, Any]]) -> list[str]:
    """Two entries for one file are two claims, and the second is unread."""
    seen: dict[str, int] = {}
    for entry in entries:
        name = str(entry.get("file"))
        seen[name] = seen.get(name, 0) + 1
    return [f"{name}: {count} entries" for name, count in sorted(seen.items()) if count > 1]


def check_no_unknown_key(entries: Sequence[dict[str, Any]]) -> list[str]:
    """A misspelt key is a requirement silently unmet, so it is an error."""
    return [
        f"{entry.get('file')}: unknown key {key!r}"
        for entry in entries
        for key in sorted(set(entry) - _ALLOWED_KEYS)
    ]


def check_the_shape_of_each_entry(entries: Sequence[dict[str, Any]]) -> list[str]:
    """The fields a claim needs to be answerable, and none that contradict it.

    A registry entry owes an identifier, a URL and a date, since without all
    three there is nothing for the networked half to re-fetch. A ``"none"``
    entry owes prose instead — nothing will ever re-fetch it, so a person
    saying what it is in writing is the only account it will ever have — and
    must carry none of the fetch fields, which would otherwise read as a
    verification that never happens.
    """
    complaints: list[str] = []
    for entry in entries:
        name, source = entry.get("file"), entry.get("source")
        if source not in _SOURCES:
            complaints.append(f"{name}: source {source!r} is not one of {sorted(_SOURCES)}")
            continue
        if source == "none":
            if not str(entry.get("note") or "").strip():
                complaints.append(f"{name}: source = 'none' and no note saying what it is")
            for field in ("id", "url", "fetched", "holds", "synthetic"):
                if field in entry:
                    complaints.append(f"{name}: source = 'none' cannot carry {field!r}")
            continue
        for field in ("url", "fetched"):
            if field not in entry:
                complaints.append(f"{name}: {source} entry with no {field!r}")
        # Retraction Watch's export is one file for the whole database, so it
        # is the one registry entry with no identifier of its own.
        if ("id" in entry) != (source != "retraction-watch"):
            complaints.append(f"{name}: {source} entry must carry 'id' iff it is not the export")
        if "holds" in entry and (source != "crossref" or entry["holds"] not in {"response", "work"}):
            complaints.append(f"{name}: 'holds' = {entry['holds']!r} is not a Crossref depth")
        if "synthetic" in entry and source != "retraction-watch":
            complaints.append(f"{name}: only the Retraction Watch export declares 'synthetic'")
    return complaints


def check_the_file_agrees_with_its_entry(entries: Sequence[dict[str, Any]]) -> list[str]:
    """The source, identifier and depth the file shows are the ones claimed.

    Coherence, and no more than that. It catches an entry and a file that have
    drifted apart — one edited, the other not — and it closes the escape hatch
    in the format itself, since ``source = "none"`` is the one value nothing
    re-fetches and a MEDLINE citation declared that way fails here.

    It would *not* have caught the fabrications in this repository's history.
    ``pubmed_wrapped_title.txt`` carried ``PMID- 28338828`` and was no efetch
    response at all (``be924a9``); an entry written from that file would have
    copied 28338828 and agreed with it perfectly. Only re-fetching settles
    that one, which is the division of labour this module is built on.
    """
    complaints: list[str] = []
    for entry in entries:
        name = str(entry.get("file"))
        path = DATA / name
        if not path.exists():
            continue
        source, identifier, holds = described_by(path)
        if source != entry.get("source"):
            complaints.append(f"{name}: declared {entry.get('source')!r}, reads as {source!r}")
            continue
        if identifier is None:
            continue
        claimed = str(entry.get("id") or "")
        if not same_identifier(source, claimed, identifier):
            complaints.append(f"{name}: declared id {claimed!r}, file carries {identifier!r}")
        if holds is not None and entry.get("holds", "response") != holds:
            complaints.append(
                f"{name}: declared holds {entry.get('holds', 'response')!r}, file is {holds!r}"
            )
    return complaints


def check_each_url_is_the_request_the_client_makes(
    entries: Sequence[dict[str, Any]],
) -> list[str]:
    """A URL nothing in ``src/`` would issue is not evidence about a registry.

    Without this the ``url`` field is free text, and the networked half would
    dutifully fetch whatever it names — a gist, a mirror, a file server — and
    report the fixture verified.
    """
    return [
        f"{entry.get('file')}: url is {entry.get('url')!r}, the client would request {wanted!r}"
        for entry in entries
        if entry.get("source") in _REGISTRIES
        and (wanted := expected_url(str(entry["source"]), entry.get("id"))) != entry.get("url")
    ]


def check_each_fetch_date_is_a_past_date(
    entries: Sequence[dict[str, Any]], today: date | None = None
) -> list[str]:
    """A TOML date, and one that has happened.

    Typed as a bare TOML date rather than a string so that ``2026-13-01``
    never parses at all, and bounded above because a fetch dated in the future
    is the one claim about provenance that can be refuted without a network.
    """
    now = today or datetime.now(UTC).date()
    complaints: list[str] = []
    for entry in entries:
        if "fetched" not in entry:
            continue
        when = entry["fetched"]
        if not isinstance(when, date) or isinstance(when, datetime):
            complaints.append(f"{entry.get('file')}: fetched = {when!r} is not a plain TOML date")
        elif when > now:
            complaints.append(f"{entry.get('file')}: fetched = {when} has not happened yet")
    return complaints


def check_declared_synthetic_rows_are_in_the_file(
    entries: Sequence[dict[str, Any]],
) -> list[str]:
    """Every ``Record ID`` declared written is a row the file really carries.

    A declaration that has outlived its row is a licence the networked check
    goes on honouring: the ID stays exempt from "must be in the live export"
    and can be spent later on a row nobody wrote it for.
    """
    complaints: list[str] = []
    for entry in entries:
        declared = entry.get("synthetic")
        if not declared:
            continue
        path = DATA / str(entry.get("file"))
        if not path.exists():
            continue
        rows = {
            row["Record ID"]
            for row in csv.DictReader(path.open(newline="", encoding="utf-8"))
            if row.get("Record ID")
        }
        complaints += [
            f"{path.name}: Record ID {rid!r} is declared synthetic and is not a row"
            for rid in declared
            if rid not in rows
        ]
    return complaints


def check_each_note_names_the_items_in_its_file(
    entries: Sequence[dict[str, Any]],
) -> list[str]:
    """A ``source = "none"`` list of items is described item by item.

    Nothing re-fetches these files and nothing else reads a note, so a note
    that describes fewer items than its file holds is wrong for as long as it
    exists. Both Zotero entries were: each said "two items" over a file of
    four, from the commit that introduced this manifest.

    The rule implemented is narrow and worth stating exactly, because it is
    weaker than the sentence above. It applies to a ``source = "none"`` file
    that parses as a JSON *list of objects*, and it requires the note to
    contain, as a substring, the ``id`` or ``key`` of every one of them. A
    file of any other shape — a ``.bib``, a Quarto page, an Obsidian note, a
    JSON object rather than a list — is not checked at all, and no wording
    beyond those identifiers is required of any note. What it catches is the
    failure that happened: items in the file that the note never mentions.
    """
    complaints: list[str] = []
    for entry in entries:
        if entry.get("source") != "none":
            continue
        path = DATA / str(entry.get("file"))
        if path.suffix != ".json" or not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            continue
        note = str(entry.get("note") or "")
        for item in payload:
            if not isinstance(item, dict):
                continue
            identifier = item.get("id") or item.get("key")
            if isinstance(identifier, str) and identifier not in note:
                complaints.append(f"{path.name}: the note does not name {identifier!r}")
    return complaints


#: Every offline check, so that adding one to this list is what runs it. Named
#: rather than collected by prefix: a check that stops being run because it was
#: renamed out of a pattern is the failure mode the whole module is about.
CHECKS: tuple[Callable[[Sequence[dict[str, Any]]], list[str]], ...] = (
    check_every_file_is_declared,
    check_no_file_is_claimed_twice,
    check_no_unknown_key,
    check_the_shape_of_each_entry,
    check_the_file_agrees_with_its_entry,
    check_each_url_is_the_request_the_client_makes,
    check_each_fetch_date_is_a_past_date,
    check_declared_synthetic_rows_are_in_the_file,
    check_each_note_names_the_items_in_its_file,
)


# ---------------------------------------------------------------------------
# Offline: the manifest as it stands.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
def test_the_manifest_passes_every_check(
    check: Callable[[Sequence[dict[str, Any]]], list[str]],
) -> None:
    """Run over the committed manifest, which must be clean."""
    assert check(ENTRIES) == []


def test_every_check_written_here_is_one_that_runs() -> None:
    """:data:`CHECKS` is what runs; a function missing from it runs nowhere.

    Each check also has its own test below proving it rejects something, and
    those call the function directly — so a check dropped out of this roster
    keeps a green test of its own while no longer looking at the manifest at
    all. That is the shape of every defect this module exists to catch, one
    level up.
    """
    defined = {
        value
        for name, value in sorted(globals().items())
        if name.startswith("check_") and callable(value)
    }

    assert defined == set(CHECKS)


def test_the_manifest_describes_the_whole_directory() -> None:
    """A count, so that a check silently reduced to an empty set still fails.

    Every function in :data:`CHECKS` iterates the entries, and all of them are
    vacuously satisfied by a manifest that has none. This is the guard on the
    guard: it does not care what the number is, only that it is what is on
    disk.
    """
    assert len(ENTRIES) == len(fixture_files()) > 0


def test_every_ignored_field_is_written_up_for_a_contributor() -> None:
    """A difference the gate declines to report has to be lookupable.

    The same contract ``benign.CHECKS`` has with ``docs/registry-artifacts.md``
    and for the same reason: a suppression a reader cannot find is one nobody
    can challenge. The list lives in the ``registry-fixtures`` skill beside the
    procedure it belongs to. Matched as the backticked field name, so a mention
    in passing does not satisfy it.
    """
    prose = RULES.read_text(encoding="utf-8")
    missing = sorted(
        field
        for field in _VOLATILE_CROSSREF | _VOLATILE_DATACITE | _VOLATILE_DATACITE_RELATIONSHIPS
        if f"`{field}`" not in prose
    )

    assert missing == []


def test_a_contributor_is_told_where_the_manifest_is_and_how_to_verify_it() -> None:
    """The instructions are the half of this that a person executes.

    Nothing here can make somebody run the networked check. What it can do is
    fail the build if the ``registry-fixtures`` skill -- the one document that
    tells a contributor how to add a fixture -- stops naming the file they must
    add an entry to, or the command that turns the entry into evidence.
    """
    prose = RULES.read_text(encoding="utf-8")

    assert "tests/data/PROVENANCE.toml" in prose
    assert "pytest -m network" in prose


# ---------------------------------------------------------------------------
# Offline: that each check would fail something.
#
# Every case below doctors one entry of the real manifest and asserts the
# complaint names the file. Without these, this module reports green on a
# manifest nobody could break and on a check that returns [] unconditionally.
# ---------------------------------------------------------------------------


def _doctored(file: str, **changes: Any) -> list[dict[str, Any]]:
    """The manifest with one entry altered; a value of ``None`` removes a key."""
    out: list[dict[str, Any]] = []
    for entry in ENTRIES:
        if entry.get("file") != file:
            out.append(dict(entry))
            continue
        altered = dict(entry)
        for key, value in changes.items():
            if value is None:
                altered.pop(key, None)
            else:
                altered[key] = value
        out.append(altered)
    return out


class TestEachCheckBites:
    def test_a_fixture_with_no_entry_is_named(self) -> None:
        kept = [entry for entry in ENTRIES if entry.get("file") != "pubmed_retracted.txt"]

        assert check_every_file_is_declared(kept) == [
            "pubmed_retracted.txt: in tests/data/ and not in the manifest"
        ]

    def test_an_entry_with_no_fixture_is_named(self) -> None:
        invented = [*ENTRIES, {"file": "pubmed_invented.txt", "source": "pubmed"}]

        assert check_every_file_is_declared(invented) == [
            "pubmed_invented.txt: in the manifest and not in tests/data/"
        ]

    def test_a_second_entry_for_one_file_is_named(self) -> None:
        doubled = [*ENTRIES, dict(ENTRIES[0])]

        assert check_no_file_is_claimed_twice(doubled) == [f"{ENTRIES[0]['file']}: 2 entries"]

    def test_a_misspelt_key_is_named(self) -> None:
        typo = _doctored("pubmed_retracted.txt", fethced="2026-08-10")

        assert check_no_unknown_key(typo) == ["pubmed_retracted.txt: unknown key 'fethced'"]

    @pytest.mark.parametrize(
        ("changes", "fragment"),
        [
            ({"url": None}, "no 'url'"),
            ({"fetched": None}, "no 'fetched'"),
            ({"id": None}, "must carry 'id'"),
            ({"source": "medline"}, "is not one of"),
            ({"holds": "work"}, "is not a Crossref depth"),
            ({"synthetic": ["1"]}, "only the Retraction Watch export"),
        ],
    )
    def test_a_registry_entry_missing_what_it_needs_is_named(
        self, changes: dict[str, Any], fragment: str
    ) -> None:
        complaints = check_the_shape_of_each_entry(_doctored("pubmed_retracted.txt", **changes))

        assert [c for c in complaints if fragment in c and c.startswith("pubmed_retracted.txt")]

    @pytest.mark.parametrize(
        ("changes", "fragment"),
        [
            ({"note": None}, "no note saying what it is"),
            ({"note": "   "}, "no note saying what it is"),
            ({"url": "https://api.crossref.org/works/10.1/x"}, "cannot carry 'url'"),
        ],
    )
    def test_an_unfetchable_entry_that_accounts_for_nothing_is_named(
        self, changes: dict[str, Any], fragment: str
    ) -> None:
        complaints = check_the_shape_of_each_entry(_doctored("sample.bib", **changes))

        assert [c for c in complaints if fragment in c and c.startswith("sample.bib")]

    def test_an_entry_naming_the_wrong_pmid_is_named(self) -> None:
        """An entry and its file, edited apart.

        Which is all this catches: had somebody written the entry *from* the
        file, the two would agree and only a fetch would tell.
        """
        wrong = _doctored(
            "pubmed_retracted.txt",
            id="20137807",
            url=expected_url("pubmed", "20137807"),
        )

        assert check_the_file_agrees_with_its_entry(wrong) == [
            "pubmed_retracted.txt: declared id '20137807', file carries '9500320'"
        ]

    def test_a_medline_record_hidden_where_nothing_refetches_it_is_named(self) -> None:
        """``source = 'none'`` is the one value the networked half skips."""
        hidden = _doctored(
            "pubmed_retracted.txt", source="none", note="not a registry response, honest",
            id=None, url=None, fetched=None,
        )

        assert check_the_file_agrees_with_its_entry(hidden) == [
            "pubmed_retracted.txt: declared 'none', reads as 'pubmed'"
        ]

    def test_a_crossref_fixture_recorded_at_the_other_depth_is_named(self) -> None:
        wrong = _doctored("compare_crossref_wakefield_retracted.json", holds="work")

        assert check_the_file_agrees_with_its_entry(wrong) == [
            "compare_crossref_wakefield_retracted.json: declared holds 'work', file is 'response'"
        ]

    def test_a_url_pointing_somewhere_else_is_named(self) -> None:
        elsewhere = _doctored(
            "pubmed_retracted.txt", url="https://gist.github.com/someone/9500320.txt"
        )
        complaints = check_each_url_is_the_request_the_client_makes(elsewhere)

        assert len(complaints) == 1
        assert complaints[0].startswith("pubmed_retracted.txt: url is 'https://gist.github.com")

    def test_a_url_naming_a_different_identifier_is_named(self) -> None:
        """The ``id`` and the ``url`` cannot drift apart and both look right."""
        mismatched = _doctored("pubmed_retracted.txt", url=expected_url("pubmed", "20137807"))

        assert len(check_each_url_is_the_request_the_client_makes(mismatched)) == 1

    @pytest.mark.parametrize(
        ("fetched", "fragment"),
        [(date(2999, 1, 1), "has not happened yet"), ("2026-08-10", "not a plain TOML date")],
    )
    def test_a_fetch_date_that_settles_nothing_is_named(
        self, fetched: object, fragment: str
    ) -> None:
        complaints = check_each_fetch_date_is_a_past_date(
            _doctored("pubmed_retracted.txt", fetched=fetched), today=date(2026, 8, 10)
        )

        assert [c for c in complaints if fragment in c]

    def test_a_synthetic_row_that_is_no_longer_in_the_file_is_named(self) -> None:
        stale = _doctored("retraction_watch_sample.csv", synthetic=["90001", "90099"])

        assert check_declared_synthetic_rows_are_in_the_file(stale) == [
            "retraction_watch_sample.csv: Record ID '90099' is declared synthetic and is not a row"
        ]

    def test_a_note_that_describes_fewer_items_than_the_file_holds_is_named(self) -> None:
        """The wording both Zotero entries shipped with, over four items."""
        undercounting = _doctored(
            "zotero_csl_export.json", note="Zotero's CSL-JSON export of two items."
        )

        assert check_each_note_names_the_items_in_its_file(undercounting) == [
            "zotero_csl_export.json: the note does not name 'papantoniou2017'",
            "zotero_csl_export.json: the note does not name 'ehbccg2002'",
            "zotero_csl_export.json: the note does not name 'molinamontes2018'",
            "zotero_csl_export.json: the note does not name 'hidalgo2015'",
        ]

    def test_a_child_item_left_out_of_a_note_is_named(self) -> None:
        """Zotero's own schema keys on ``key``, not ``id``, and an attachment
        and a note are items of the file like any other.
        """
        parents_only = _doctored(
            "zotero_native_items.json", note="Zotero's own item JSON for ABCD1234 and EFGH5678."
        )

        assert check_each_note_names_the_items_in_its_file(parents_only) == [
            "zotero_native_items.json: the note does not name 'PDF00001'",
            "zotero_native_items.json: the note does not name 'NOTE0001'",
        ]


class TestReadingAFixturesOwnAccountOfItself:
    """The readers :func:`described_by` is built from, each on a real file."""

    def test_a_medline_response_yields_its_pmid(self) -> None:
        text = (DATA / "pubmed_retracted.txt").read_text(encoding="utf-8")

        assert medline_pmid(text) == "9500320"

    def test_prose_that_merely_mentions_a_pmid_yields_nothing(self) -> None:
        """``PMID- `` at the start of a line, not the four letters anywhere."""
        assert medline_pmid("see PMID- 9500320 in the notes") is None

    def test_a_crossref_response_and_a_bare_work_are_told_apart(self) -> None:
        whole = json.loads(
            (DATA / "compare_crossref_wakefield_retracted.json").read_text(encoding="utf-8")
        )
        work = json.loads(
            (DATA / "audit_crossref_interleaved_collective.json").read_text(encoding="utf-8")
        )

        assert crossref_doi(whole) == ("10.1016/s0140-6736(97)11096-0", "response")
        assert crossref_doi(work) == ("10.1158/1055-9965.epi-23-0009", "work")

    def test_a_datacite_response_yields_its_doi(self) -> None:
        payload = json.loads((DATA / "datacite_journal_article.json").read_text(encoding="utf-8"))

        assert datacite_doi(payload) == "10.24377/dteij.article3641"

    @pytest.mark.parametrize(
        "name", ["sample.bib", "zotero_native_items.json", "markdown_obsidian_note.md"]
    )
    def test_a_bibliography_the_tool_reads_is_not_mistaken_for_an_answer(self, name: str) -> None:
        """Both zotero fixtures are JSON *arrays*, which no registry returns here."""
        assert described_by(DATA / name) == ("none", None, None)

    def test_the_retraction_watch_export_is_recognised_by_its_columns(self) -> None:
        assert described_by(DATA / "retraction_watch_sample.csv") == (
            "retraction-watch",
            None,
            None,
        )

    def test_a_doi_recorded_in_the_other_case_is_the_same_doi(self) -> None:
        """Crossref answers in lower case and a bibliography rarely does."""
        assert same_identifier("crossref", "10.1016/S0140-6736(97)11096-0", "10.1016/s0140-6736(97)11096-0")
        assert not same_identifier("pubmed", "9500320", "09500320")


# ---------------------------------------------------------------------------
# Networked: the half that establishes anything.
# ---------------------------------------------------------------------------


def _strip(payload: dict[str, Any], volatile: frozenset[str]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in volatile}


def _datacite_comparable(payload: dict[str, Any]) -> dict[str, Any]:
    """*payload* with DataCite's own counters and timestamps taken out."""
    data = dict(payload.get("data") or {})
    data["attributes"] = _strip(dict(data.get("attributes") or {}), _VOLATILE_DATACITE)
    data["relationships"] = _strip(
        dict(data.get("relationships") or {}), _VOLATILE_DATACITE_RELATIONSHIPS
    )
    return {**payload, "data": data}


def _differing_keys(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    """The top-level keys to name in a failure, so a diff is readable."""
    return sorted(key for key in set(left) | set(right) if left.get(key) != right.get(key))


@pytest.fixture(scope="module")
def live() -> Client:
    """A client with no cache, so every fetch here is a fetch.

    ``Client`` rather than ``urllib`` for the User-Agent, the per-host
    throttle and the retry budget: these tests issue forty-odd requests to two
    NCBI and Crossref endpoints in a row, and a fixture check is not a reason
    to be impolite to a registry.
    """
    return Client(cache=None)


@pytest.mark.network
@pytest.mark.parametrize("entry", _of_source("pubmed"), ids=_ids(_of_source("pubmed")))
def test_a_medline_fixture_is_what_efetch_returns_today(
    entry: dict[str, Any], live: Client
) -> None:
    """Byte for byte, blank lines and NLM's own line wrapping included.

    Nothing is normalised away. Every one of these files is a whole ``efetch``
    response, so any difference at all is either NLM revising the record — in
    which case the fix is to re-fetch and commit, and the diff is worth
    reading — or the file not being what it says it is.
    """
    answer = live.get_text(str(entry["url"]))

    assert answer is not None, f"{entry['file']}: efetch has no record for PMID {entry['id']}"
    assert answer == (DATA / str(entry["file"])).read_text(encoding="utf-8")


@pytest.mark.network
@pytest.mark.parametrize("entry", _of_source("crossref"), ids=_ids(_of_source("crossref")))
def test_a_crossref_fixture_is_what_the_api_returns_today(
    entry: dict[str, Any], live: Client
) -> None:
    """Compared as parsed JSON, since a recorded ``message`` cannot be bytes."""
    answer = live.get_json(str(entry["url"]))
    assert answer is not None, f"{entry['file']}: Crossref has no work for {entry['id']}"

    if entry.get("holds") == "work":
        answer = answer["message"]
    stored = json.loads((DATA / str(entry["file"])).read_text(encoding="utf-8"))
    live_work = answer.get("message", answer)
    stored_work = stored.get("message", stored)

    assert _strip(stored_work, _VOLATILE_CROSSREF) == _strip(live_work, _VOLATILE_CROSSREF), (
        f"{entry['file']}: {_differing_keys(stored_work, live_work)}"
    )


@pytest.mark.network
@pytest.mark.parametrize("entry", _of_source("datacite"), ids=_ids(_of_source("datacite")))
def test_a_datacite_fixture_is_what_the_api_returns_today(
    entry: dict[str, Any], live: Client
) -> None:
    answer = live.get_json(str(entry["url"]))
    assert answer is not None, f"{entry['file']}: DataCite has no record for {entry['id']}"

    stored = json.loads((DATA / str(entry["file"])).read_text(encoding="utf-8"))

    comparable_stored = _datacite_comparable(stored)
    comparable_answer = _datacite_comparable(answer)

    # Named from the compared dicts, not the raw ones: a message listing a
    # field this check ignores sends the reader after a counter that did not
    # fail anything.
    assert comparable_stored == comparable_answer, (
        f"{entry['file']}: "
        f"{_differing_keys(comparable_stored['data']['attributes'], comparable_answer['data']['attributes'])}"
    )


@pytest.mark.network
@pytest.mark.parametrize(
    "entry", _of_source("retraction-watch"), ids=_ids(_of_source("retraction-watch"))
)
def test_the_retraction_watch_extract_is_rows_of_the_live_export(
    entry: dict[str, Any], live: Client
) -> None:
    """Both halves of the claim the manifest makes about this one file.

    Every row not declared written must be in today's export with all twenty
    columns equal, and every row that *is* declared written must be in it
    under no ``Record ID`` at all — RW assigns those numbers, and one of them
    landing on a real retraction would turn a fixture nobody re-reads into a
    row claiming to be about a real paper.

    This downloads the whole export, tens of megabytes of it. That is the
    price of checking an extract against the thing it was extracted from, and
    it is why this test carries the marker.
    """
    text = live.get_text(str(entry["url"]))
    assert text, f"{entry['file']}: the export answered with nothing"

    export = {row["Record ID"]: row for row in csv.DictReader(io.StringIO(text))}
    written = set(entry.get("synthetic") or [])
    stored = list(csv.DictReader((DATA / str(entry["file"])).open(newline="", encoding="utf-8")))
    assert stored, f"{entry['file']}: no rows"

    copied = [row for row in stored if row["Record ID"] not in written]
    assert [row["Record ID"] for row in copied if row["Record ID"] not in export] == []
    assert [rid for rid in sorted(written) if rid in export] == []
    assert [
        f"{row['Record ID']}: {column}"
        for row in copied
        for column in row
        if row[column] != export[row["Record ID"]].get(column)
    ] == []
