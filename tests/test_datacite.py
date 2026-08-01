"""DataCite registry client.

Offline only. :class:`_StubClient` stands in for
:class:`~bibaudit.registries.http.Client`, and every response it serves was
recorded verbatim from ``https://api.datacite.org/dois/<doi>`` into
``tests/data/datacite_*.json``. An autouse tripwire replaces
``urllib.request.urlopen`` for the whole module, so a client that opened its
own socket fails here instead of quietly working on a maintainer's laptop and
failing in CI.

The recorded responses are kept exactly as the API returned them, defects
included. ``datacite_creator_name_only.json`` is the clearest example: its
title reads ``dell?assistenza`` where the deposit meant a right single quote,
and its ``publisher`` carries a literal U+FFFD replacement character
(``Universit<U+FFFD> di Modena``). Both are DataCite's own encoding damage,
recorded on purpose — "tidying" them up would delete the reason the fixture is
a real response rather than a hand-written dict.

The DOIs behind the fixtures, each fetched live before being written down:

``datacite_arxiv_preprint.json``
    ``10.48550/arXiv.1706.03762`` — arXiv mints through DataCite, not
    Crossref, which is the whole reason this client exists. Eight creators
    with explicit ``familyName``/``givenName``.
``datacite_journal_article.json``
    ``10.24377/dteij.article3641`` — a journal article registered with
    DataCite, so it is the one fixture that carries a populated ``container``.
``datacite_organizational_creator.json``
    ``10.15468/dl.pqqnhb`` — a GBIF occurrence download, creator
    ``GBIF.org User`` marked ``nameType: "Organizational"``.
``datacite_creator_given_name_only.json``
    ``10.34760/6a69ef00bbbef`` — three creators marked
    ``nameType: "Personal"`` with ``givenName`` set and ``familyName`` an
    explicit ``null``.
``datacite_creator_name_only.json``
    ``10.25431/11380_1192850`` — one ``nameType: "Personal"`` creator given
    only as the string ``"Risso, G. L."``.
``datacite_alternative_title_first.json``
    ``10.26268/heal.uoi.19814`` — a University of Ioannina thesis whose
    ``titles`` array puts the Greek ``AlternativeTitle`` *first* and the
    untyped primary title second, which is what separates "the title" from
    "``titles[0]``".

A handful of tests derive a payload from one of those recordings (deleting a
``lastPage``, tagging a title, corrupting one creator) to reach a branch no
recorded response happens to exercise. Each says so, and each starts from a
real response rather than a hand-written dict, so the surrounding fields stay
exactly as DataCite serves them. Exactly one body here is not DataCite's at
all — the "200 that is not a DataCite record" in
``TestAbsenceAndOutage``, which by definition cannot be recorded from
DataCite.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from bibaudit.compare import compare
from bibaudit.model import Name, Record, Reference
from bibaudit.names import compare_author_lists
from bibaudit.registries.datacite import DataCite
from bibaudit.registries.http import Transient

_DATA = Path(__file__).parent / "data"

_BASE = "https://api.datacite.org/dois/"

# Normalised (lowercase) forms of the fixture DOIs. Written out literally
# rather than computed with `normalize_doi`, so a test that asserts the result
# is keyed by the normalised DOI is not comparing the code against itself.
_ARXIV = "10.48550/arxiv.1706.03762"
_JOURNAL = "10.24377/dteij.article3641"
_GBIF = "10.15468/dl.pqqnhb"
_GIVEN_ONLY = "10.34760/6a69ef00bbbef"
_NAME_ONLY = "10.25431/11380_1192850"
_GREEK_THESIS = "10.26268/heal.uoi.19814"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn any real HTTP call made from this module into a hard failure.

    ``datacite.py`` is only allowed to reach the network through the injected
    :class:`~bibaudit.registries.http.Client`, because that is what supplies
    the on-disk cache, the per-host throttle, and the "404 is a fact, a
    timeout is ignorance" distinction. A client that called ``urlopen``
    directly would satisfy every other assertion in this file while bypassing
    all three, and the default test run would start needing the internet.
    """

    def _tripwire(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "datacite.py opened a socket instead of going through the injected Client"
        )

    monkeypatch.setattr(urllib.request, "urlopen", _tripwire)


class _StubClient:
    """Stands in for :class:`~bibaudit.registries.http.Client`.

    *responder* maps a request URL to what ``get_json`` should hand back:
    a decoded payload, ``None`` for a confirmed HTTP 404, or a raised
    :class:`~bibaudit.registries.http.Transient` for an outage. Every URL
    asked for is recorded in :attr:`urls`.

    Any attribute other than ``get_json`` raises rather than being invented
    on demand. The point of injecting a client is that it is the *only* door
    to the network; a stub that grew a ``.session`` or a ``.get_text`` the
    moment production code asked for one would let a second, uncached,
    unthrottled transport slip in unnoticed.
    """

    def __init__(self, responder: Callable[[str], dict[str, Any] | None]) -> None:
        self._responder = responder
        self.urls: list[str] = []

    def get_json(
        self,
        url: str,
        *,
        cache_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        self.urls.append(url)
        return self._responder(url)

    def __getattr__(self, attr: str) -> Any:
        raise AssertionError(
            f"datacite.py reached for Client.{attr}, which this stub deliberately does not model"
        )


def _recorded(name: str) -> dict[str, Any]:
    """One verbatim recorded DataCite response from ``tests/data``."""
    with (_DATA / f"datacite_{name}.json").open(encoding="utf-8") as handle:
        payload: dict[str, Any] = json.load(handle)
    return payload


def _client(routes: Mapping[str, dict[str, Any] | Exception | None]) -> _StubClient:
    """A stub answering per DOI, keyed by the DOI in the request path.

    An unrouted DOI is an assertion failure rather than a 404: if the client
    ever asks for something other than what the test set up — a DOI that lost
    its case normalisation, say — that must surface as a failure and not as a
    plausible-looking "DataCite does not have this work".

    The path is percent-decoded before routing, the way the server does it, so
    that routing keys stay plain DOIs; whether the client encoded them
    correctly is asserted where it matters, in
    ``test_a_doi_is_percent_encoded_into_the_request_path``, rather than being
    silently required of every test here.
    """

    def responder(url: str) -> dict[str, Any] | None:
        doi = urllib.parse.unquote(url[len(_BASE) :])
        if doi not in routes:
            raise AssertionError(f"unexpected DataCite request: {url!r}")
        answer = routes[doi]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return _StubClient(responder)


def _record(fixture: str, doi: str) -> Record:
    """The :class:`Record` DataCite builds for *doi* from a recorded response.

    *doi* is the already-normalised DOI: both the routing key of the stub and
    the key the result is expected under.
    """
    client = _client({doi: _recorded(fixture)})
    return DataCite(client).by_dois([doi])[doi]


def _record_from(payload: dict[str, Any], doi: str) -> Record:
    """The :class:`Record` DataCite builds for *doi* from an arbitrary payload.

    For the derived responses described in the module docstring: same path
    through the injected client as :func:`_record`, but the caller supplies the
    (usually recorded, then deliberately damaged) payload.
    """
    client = _client({doi: payload})
    return DataCite(client).by_dois([doi])[doi]


class TestRecordMapping:
    """``data.attributes`` onto :class:`~bibaudit.model.Record`."""

    def test_title_comes_from_the_titles_array(self) -> None:
        assert _record("arxiv_preprint", _ARXIV).title == "Attention Is All You Need"

    def test_the_title_is_the_untyped_entry_not_merely_the_first_one(self) -> None:
        """DataCite's primary title is the one carrying no ``titleType``.

        The University of Ioannina's HEAL repository deposits this thesis with
        its Greek title tagged ``AlternativeTitle`` *first* and the untyped
        English title second. Reading ``titles[0]`` would compare a
        bibliography's English title against a Greek string — similarity near
        zero — and report WRONG-WORK, the harshest verdict the tool has, for a
        citation that is exactly right.
        """
        record = _record("alternative_title_first", _GREEK_THESIS)
        assert record.title == "Study of intracranial tumors using histogram analysis"

    def test_a_record_whose_titles_are_all_type_tagged_still_reports_one(self) -> None:
        """A type-tagged title beats no title at all.

        Derived from the HEAL response by tagging its untyped English title
        ``TranslatedTitle`` as well — a shape the DataCite schema permits.
        Returning ``None`` because no entry is the designated primary would
        leave the record titleless, and a registry answer with no title
        corroborates nothing: the reference would be reported UNCONFIRMED
        although DataCite plainly holds the work.
        """
        payload = _recorded("alternative_title_first")
        for entry in payload["data"]["attributes"]["titles"]:
            entry["titleType"] = entry.get("titleType") or "TranslatedTitle"
        record = _record_from(payload, _GREEK_THESIS)
        assert record.title == "Μελέτη των ενδοκράνιων όγκων με ανάλυση ιστογράμματος"

    def test_the_title_is_stored_as_display_text(self) -> None:
        """``clean()`` on the way in: a report shows text, not registry markup.

        DataCite deposits carry HTML (``<i>``, ``&amp;``) and typographic
        punctuation in ``titles``. This recorded response exercises the
        punctuation half: the recorded title spells "Isn't" with a U+2019
        apostrophe, and the assertion below pins the cleaned, straight-quoted
        form it is displayed as. Storing the raw string instead would leave a
        report printing the registry's markup at the reader, in exactly the
        field they are being asked to compare by eye.
        """
        assert _record("journal_article", _JOURNAL).title == (
            "The Name You Can Say Isn't the Real Name: "
            "A Typology of Ideation for Design and Technology Education"
        )

    def test_registry_encoding_damage_is_carried_through_unrepaired(self) -> None:
        """Report, never rewrite — including when the registry is the broken one.

        IRIS returned this title with the deposit's right single quote already
        destroyed: ``dell?assistenza``. Silently repairing it here would hide
        from the report the exact glyph that makes the title comparison
        differ, and the verdict would stop being reproducible from the cached
        response. Deciding that a difference is the registry's fault is
        ``benign.py``'s job, at comparison time, where it is recorded as
        REGISTRY-ARTIFACT instead of being erased.
        """
        record = _record("creator_name_only", _NAME_ONLY)
        assert record.title == (
            "Un sistema di gestione dei servizi per il lavoro nel settore "
            "dell?assistenza familiare"
        )

    def test_the_record_names_the_registry_that_answered(self) -> None:
        """``Record.source`` is how a finding is attributed to a registry.

        ``compare.py`` stamps it onto every ``Issue`` it raises, ``report.py``
        prints it beside the registry's value, and ``suggest.py`` names it in
        the header of the file it writes. Mislabelled, DataCite's
        metadata is credited to Crossref — and the reader who goes to check
        the Crossref record finds the quoted value is not there.
        """
        assert DataCite.name == "datacite"
        assert _record("arxiv_preprint", _ARXIV).source == "datacite"

    def test_publication_year_is_filed_as_the_issued_year(self) -> None:
        """DataCite has one date; it must land in a slot the comparison reads.

        ``compare._check_year`` accepts any year in ``Record.years``, but it
        can only do that for years that are *in* the mapping. A
        ``publicationYear`` left out (or kept as the string ``"2017"``) makes
        every arXiv, Zenodo and figshare citation look like a work whose year
        nothing can corroborate.
        """
        record = _record("arxiv_preprint", _ARXIV)
        assert record.years == {"issued": 2017}
        assert record.year == 2017

    def test_resource_type_general_is_kept_as_the_registrys_own_string(self) -> None:
        """``Record.kind`` holds what DataCite said, not the folded vocabulary.

        Normalisation happens once, in ``compare.normalize_kind``. Doing it
        here as well would leave the report printing ``preprint`` when the
        user needs to see that DataCite actually registered the work as
        ``Preprint`` — and a registry string this tool has no mapping for
        (DataCite's ``Text``, ``StudyRegistration``) would be flattened to
        ``other`` before anyone could tell it apart from a genuine ``Other``.
        """
        assert _record("arxiv_preprint", _ARXIV).kind == "Preprint"

    def test_container_title_becomes_the_record_container(self) -> None:
        """The journal name comes from ``container``, not from ``publisher``.

        On this recorded response the two strings happen to be identical —
        journals that deposit through OJS routinely set ``publisher`` to their
        own name — so this assertion alone cannot tell the two fields apart.
        ``test_a_deposit_with_no_container_has_no_journal_fields`` is the one
        that does.
        """
        record = _record("journal_article", _JOURNAL)
        assert record.container == "Design and Technology Education: An International Journal"

    def test_container_volume_and_issue_are_carried_across(self) -> None:
        record = _record("journal_article", _JOURNAL)
        assert (record.volume, record.issue) == ("31", "2")

    def test_first_and_last_page_are_joined_into_a_range(self) -> None:
        """DataCite splits the page span; ``Record.pages`` is one display string.

        Only ``normalize.first_page`` of it is ever compared, but the report
        shows the whole thing, and a record that carried only ``323`` would
        make a bibliography's correct ``323--343`` look like it had invented
        the closing page.
        """
        assert _record("journal_article", _JOURNAL).pages == "323-343"

    def test_a_deposit_with_only_a_first_page_is_not_padded_into_a_range(self) -> None:
        """Half a span is one page, not a span with a hole in it.

        Derived from the journal response by deleting ``lastPage``, which many
        DataCite journal deposits simply never carry. Only
        ``normalize.first_page`` of this value is compared, so the damage
        would be invisible to the comparison and visible everywhere else: a
        report line reading ``pages: 323-None`` beside a bibliography's
        ``323``, inviting a correction that would make the entry wrong.
        """
        payload = _recorded("journal_article")
        del payload["data"]["attributes"]["container"]["lastPage"]
        assert _record_from(payload, _JOURNAL).pages == "323"

    def test_a_deposit_with_no_container_has_no_journal_fields(self) -> None:
        """An arXiv preprint has no journal, and the record must say so.

        The recorded response carries ``"container": {}`` while
        ``publisher`` is ``"arXiv"`` — which is what makes this the test that
        separates the two fields. A ``Record.container`` sourced from
        ``publisher`` would report every arXiv preprint as published in a
        journal called *arXiv*, and then accuse a bibliography that leaves
        ``journal`` empty (correctly) of an incomplete entry. The empty
        ``container`` object must also yield ``None``, not the empty strings a
        report would render as a blank ``volume:`` line.
        """
        record = _record("arxiv_preprint", _ARXIV)
        assert record.raw["publisher"] == "arXiv"
        assert (record.container, record.volume, record.issue, record.pages) == (
            None,
            None,
            None,
            None,
        )


class TestCreators:
    def test_an_organisational_creator_stays_one_literal_creator(self) -> None:
        """``nameType: "Organizational"`` is DataCite's own, authoritative signal.

        ``GBIF.org User`` contains none of ``names._COLLECTIVE_MARKERS``, so
        the surname heuristic reads it as a person called *User* with the
        forename *GBIF.org*. Every GBIF occurrence download in a bibliography
        would then be reported as citing an author who does not exist.
        """
        authors = _record("organizational_creator", _GBIF).authors
        assert len(authors) == 1
        assert authors[0].collective is True
        assert authors[0].literal == "GBIF.org User"
        assert authors[0].family == ""

    def test_a_personal_creator_with_a_given_name_but_no_family_name_is_a_person(self) -> None:
        """Regression. Absent ``familyName`` does not mean "organisation".

        Every creator in this recorded response is marked
        ``nameType: "Personal"`` and carries ``"familyName": null`` with the
        whole name in ``givenName``/``name`` — the University Medical Center
        Groningen deposits them that way. Treating a missing ``familyName``
        as a collective author turned three real people into three
        organisations, which both erased their surnames from the report and
        (see the next test) silently switched the author check off.
        """
        authors = _record("creator_given_name_only", _GIVEN_ONLY).authors
        assert [n.collective for n in authors] == [False, False, False]
        assert [n.family for n in authors] == ["Olivier", "Pyott", "Jagersma"]
        assert [n.given for n in authors] == ["Jocelien", "Sonja", "Joelle"]

    def test_a_lone_personal_creator_given_as_a_string_still_gets_compared(self) -> None:
        """A false collective author voids the author check entirely.

        ``compare_author_lists`` treats a single collective creator on either
        side as a representation difference and returns clean — correct for a
        real consortium byline, catastrophic for ``"Risso, G. L."``, a person
        whom IRIS deposits as a bare ``name`` string. Misclassified, this
        reference would pass the author comparison no matter whose name the
        bibliography stored.
        """
        record = _record("creator_name_only", _NAME_ONLY)
        assert record.authors == [Name(family="Risso", given="G. L.")]

        wrong_author = compare_author_lists([Name(family="Rossi", given="G.")], record.authors)
        assert wrong_author.mismatches, "a substituted surname went unreported"

    def test_a_creator_with_no_name_type_at_all_is_parsed_as_a_person(self) -> None:
        """``nameType`` is optional, and most journal deposits omit it.

        This recorded response has a single creator ``{"name": "McLain,
        Matt", ...}`` with neither ``nameType`` nor ``familyName``. Defaulting
        an absent ``nameType`` to organisational would collapse most
        DataCite-registered journal articles into collective authors.
        """
        authors = _record("journal_article", _JOURNAL).authors
        assert authors == [Name(family="McLain", given="Matt")]

    def test_a_creator_entry_that_is_not_an_object_costs_only_that_creator(self) -> None:
        """One unusable creator must not abort the whole audit.

        Derived from the arXiv response by replacing one creator object with
        the bare string DataCite's own schema forbids. Without the guard,
        ``.get`` on a ``str`` raises ``AttributeError`` — and ``by_dois``
        catches nothing, so a single malformed deposit anywhere in a
        bibliography ends the run with a traceback instead of a report.

        What it costs is a one-short author list, which can raise an
        author-count warning on an entry that is correct; that is why the
        branch is reserved for a shape no valid deposit can have.
        """
        payload = _recorded("arxiv_preprint")
        payload["data"]["attributes"]["creators"][1] = "Shazeer, Noam"
        record = _record_from(payload, _ARXIV)
        assert [n.family for n in record.authors] == [
            "Vaswani",
            "Parmar",
            "Uszkoreit",
            "Jones",
            "Gomez",
            "Kaiser",
            "Polosukhin",
        ]

    def test_creator_order_is_preserved(self) -> None:
        """Authorship order is data, and the comparison is positional.

        ``compare_author_lists`` pairs stored and registry creators by index,
        so reordering them here would report a first-author swap on a
        bibliography that has the byline exactly right.
        """
        record = _record("arxiv_preprint", _ARXIV)
        assert [n.family for n in record.authors] == [
            "Vaswani",
            "Shazeer",
            "Parmar",
            "Uszkoreit",
            "Jones",
            "Gomez",
            "Kaiser",
            "Polosukhin",
        ]


def _reference_agreeing_with(record: Record, key: str, **overrides: Any) -> Reference:
    """A Reference that matches *record* on every field ``compare`` checks.

    Built from the record so the comparison has exactly one variable in it:
    without this, a hand-written Reference missing its authors and year
    produces its own issues and the assertion about the field under test
    passes or fails for the wrong reason.
    """
    ref = Reference(
        key=key,
        locator="references.bib:1",
        kind=record.kind or "other",
        doi=record.doi,
        title=record.title,
        authors=list(record.authors),
        year=record.year,
        container=record.container,
        volume=record.volume,
        issue=record.issue,
        pages=record.pages,
    )
    for name, value in overrides.items():
        setattr(ref, name, value)
    return ref


class TestPublisherIsDeliberatelyNotMapped:
    """``attributes.publisher`` is read, kept in ``raw``, and not compared.

    Every DataCite response carries it, and ``compare._check_scalar`` already
    has a ``publisher`` clause waiting — it just returns early because
    ``Record.publisher`` is ``None``. Filling it in is a one-line change that
    switches a *new field-level check on for every DataCite-resolved reference
    at once*, and rule 3 says a check ships with its known-benign exceptions
    or it does not ship. ``benign.py`` has no publisher clause of any kind,
    and this module may not add one.

    The three tests that assert what *would* happen exist because "we decided
    not to" is worth nothing to the next reader without the evidence attached.
    They construct the mapped record by hand and run the real comparison, so
    the day someone adds the ``benign`` rules and flips the mapping on, these
    tests fail loudly and say exactly which cases the new rules have to cover
    — rather than the fixtures quietly agreeing with whatever was shipped.
    """

    def test_the_record_carries_no_publisher(self) -> None:
        assert _record("journal_article", _JOURNAL).publisher is None
        assert _record("arxiv_preprint", _ARXIV).publisher is None
        assert _record("organizational_creator", _GBIF).publisher is None

    def test_the_registrys_value_is_still_reachable_through_raw(self) -> None:
        """Not mapped is not discarded.

        ``Record.raw`` holds ``attributes`` verbatim, so a future check, a
        ``--json`` consumer, or a human reading the cached response loses
        nothing by this decision. Withholding the comparison is the whole of
        it.
        """
        record = _record("journal_article", _JOURNAL)
        assert record.raw["publisher"] == "Design and Technology Education: An International Journal"

    def test_mapping_it_would_fail_a_correct_bibliography(self) -> None:
        """The concrete false alarm, on a real recorded response.

        DataCite defines ``publisher`` as whoever holds, archives, publishes,
        prints, distributes, releases, issues or produces the resource — so an
        OJS journal deposits its *own title* there.
        ``10.24377/dteij.article3641`` returns "Design and Technology
        Education: An International Journal", which is the journal, already
        checked as ``container``. Whatever a bibliography records as this
        article's publisher — the Design and Technology Association that owns
        the journal, or Liverpool John Moores University, whose OJS instance
        the deposit's own ``url`` points at — it is not the journal's title.

        The result is not a note or a warning: ``_check_scalar`` grades an
        unexplained scalar difference ``error``, and ``verdict_for`` turns any
        error into ``FIELD-MISMATCH``, which is in ``model.FAILING_VERDICTS``.
        A correct entry would fail somebody's CI.
        """
        record = _record("journal_article", _JOURNAL)
        record.publisher = record.raw["publisher"]  # what the one-line change does
        ref = _reference_agreeing_with(
            record, "dteij2026names", publisher="Liverpool John Moores University"
        )
        result = compare(ref, {"datacite": record})
        assert result.verdict == "FIELD-MISMATCH"
        assert result.fails is True
        assert [(i.field, i.kind, i.severity) for i in result.issues] == [
            ("publisher", "mismatch", "error")
        ]

    def test_mapping_it_would_report_every_preprint_incomplete(self) -> None:
        """The quieter, larger half of the same false alarm.

        ``10.48550/arXiv.1706.03762`` publishes as "arXiv". Nobody writes
        ``publisher = {arXiv}`` in a ``@misc`` preprint entry, so the missing
        branch of ``_check_scalar`` fires instead of the mismatch branch and
        every DataCite-resolved preprint in a bibliography turns
        ``INCOMPLETE``. That does not fail a build, which is exactly what
        makes it dangerous: it is pure volume in the report, and a report
        nobody finishes reading is the failure mode rule 3 names.
        """
        record = _record("arxiv_preprint", _ARXIV)
        record.publisher = record.raw["publisher"]
        ref = _reference_agreeing_with(record, "vaswani2017attention")
        assert ref.publisher is None  # as a @misc preprint entry really is
        result = compare(ref, {"datacite": record})
        assert result.verdict == "INCOMPLETE"
        assert [(i.field, i.kind, i.severity) for i in result.issues] == [
            ("publisher", "missing", "warning")
        ]

    def test_a_genuinely_wrong_journal_is_still_caught(self) -> None:
        """The true positive that must survive the withheld check.

        This is the pairing CLAUDE.md demands, in its awkward direction: the
        change above makes the tool say *less*, so what has to be proved is
        that the field the publisher check would have overlapped with still
        fires. Citing this article to the wrong journal — the mistake a
        publisher check would supposedly have caught, since DataCite files the
        journal title under ``publisher`` — is caught by ``container``, at the
        same severity and with the same failing verdict, with no help from
        ``publisher`` at all.
        """
        record = _record("journal_article", _JOURNAL)
        assert record.publisher is None  # the check under discussion stays off
        ref = _reference_agreeing_with(
            record,
            "dteij2026names",
            container="International Journal of Technology and Design Education",
        )
        result = compare(ref, {"datacite": record})
        assert result.verdict == "FIELD-MISMATCH"
        assert ("container", "mismatch", "error") in [
            (i.field, i.kind, i.severity) for i in result.issues
        ]


class TestAbsenceAndOutage:
    """A 404 is a fact about the work; a failure to ask is not."""

    def test_a_404_is_a_confirmed_absence_and_yields_no_record(self) -> None:
        """Most DOIs are simply not DataCite's, and that is the normal case.

        DataCite is consulted only for the DOIs Crossref did not answer for,
        so 404 is the majority outcome. Raising on it would abort an audit
        over an entirely ordinary Crossref-registered reference.
        """
        client = _client({"10.1000/not-in-datacite": None})
        assert DataCite(client).by_dois(["10.1000/not-in-datacite"]) == {}
        assert len(client.urls) == 1

    def test_a_404_on_one_doi_does_not_discard_the_others(self) -> None:
        client = _client({"10.1000/absent": None, _ARXIV: _recorded("arxiv_preprint")})
        result = DataCite(client).by_dois(["10.1000/absent", _ARXIV])
        assert list(result) == [_ARXIV]

    def test_a_200_this_client_cannot_read_yields_no_record_rather_than_a_crash(self) -> None:
        """A response with no ``data.attributes`` must not take the batch down.

        The realistic source is not DataCite but something in front of it — a
        proxy or a captive portal answering 200 with JSON of its own. Without
        the shape check, ``attributes`` is ``None`` and the next attribute
        access raises, ending the audit at whichever reference happened to be
        first.

        Note what this pins and what it does not: no traceback, and no
        half-built ``Record`` whose empty title would be compared against the
        entry's real one. It is *not* a statement that an unreadable 200 is a
        confirmed absence — this client currently cannot tell the caller the
        difference, which is recorded as a known limitation rather than
        blessed here.
        """
        client = _client(
            {
                "10.1000/unreadable": {"errors": [{"title": "not a DataCite record"}]},
                _ARXIV: _recorded("arxiv_preprint"),
            }
        )
        result = DataCite(client).by_dois(["10.1000/unreadable", _ARXIV])
        assert list(result) == [_ARXIV]

    def test_a_transient_propagates_instead_of_looking_like_an_absence(self) -> None:
        """An outage midway must not leave a partial result behind.

        Swallowing :class:`Transient` here would return a dict missing the
        DOIs that were never successfully asked about, and the caller cannot
        tell that apart from "DataCite does not have them" — which is how a
        registry outage gets reported as a bibliography full of unconfirmable
        references.
        """
        client = _client(
            {
                _ARXIV: _recorded("arxiv_preprint"),
                "10.1000/during-the-outage": Transient("datacite unreachable"),
            }
        )
        with pytest.raises(Transient):
            DataCite(client).by_dois([_ARXIV, "10.1000/during-the-outage"])


class TestRequests:
    """Everything goes through the injected client, once, per normalised DOI."""

    def test_every_lookup_goes_through_the_injected_client(self) -> None:
        """No transport of its own: the cache and the throttle live in Client.

        The stub raises on any attribute but ``get_json``, and the module-wide
        autouse fixture makes ``urlopen`` fatal, so the single recorded URL
        below is the only way this lookup could have been served.
        """
        client = _client({_ARXIV: _recorded("arxiv_preprint")})
        DataCite(client).by_dois([_ARXIV])
        assert client.urls == [f"{_BASE}{_ARXIV}"]

    def test_the_network_tripwire_is_armed(self) -> None:
        """Guards the guard.

        Every other test in this file only proves the network was untouched
        if ``_no_network`` actually patched the symbol ``http.py`` calls. A
        typo in the monkeypatch target would silently disarm it and nothing
        else here would notice.
        """
        with pytest.raises(AssertionError):
            urllib.request.urlopen(f"{_BASE}{_ARXIV}")

    def test_the_doi_is_normalised_for_both_the_request_and_the_result_key(self) -> None:
        """arXiv prints its DOI as ``10.48550/arXiv.…``; DOIs are caseless.

        ``audit.py`` looks the returned records up by the reference's own
        normalised DOI. A record keyed by the stored spelling would never be
        found, and the citation would be reported ``BAD-ID`` — "resolves in no
        consulted registry" — for a DOI that resolves perfectly well.
        """
        client = _client({_ARXIV: _recorded("arxiv_preprint")})
        result = DataCite(client).by_dois(["https://doi.org/10.48550/arXiv.1706.03762"])
        assert list(result) == [_ARXIV]
        assert result[_ARXIV].doi == _ARXIV
        assert client.urls == [f"{_BASE}{_ARXIV}"]

    def test_the_same_doi_written_two_ways_is_requested_once(self) -> None:
        """One request per work, not per citation of it.

        A review bibliography cites the same dataset from several entries;
        without deduplication each one costs a throttled round trip.
        """
        client = _client({_ARXIV: _recorded("arxiv_preprint")})
        DataCite(client).by_dois(
            ["10.48550/arXiv.1706.03762", _ARXIV, "doi:10.48550/ARXIV.1706.03762"]
        )
        assert len(client.urls) == 1

    def test_a_doi_is_percent_encoded_into_the_request_path(self) -> None:
        """DOIs contain characters a URL reads as structure.

        ``10.1016/S0140-6736(03)14065-2`` is an ordinary Lancet DOI, and
        ``normalize_doi`` hands over whatever the bibliography stored — a
        stray space or ``#`` included, since guessing at a malformed DOI is
        not this client's job. Interpolated raw, a space makes ``urlopen``
        build a malformed request, and a ``#`` or ``?`` truncates the path so
        the lookup silently asks about a *different* DOI and reports that
        work's metadata as this reference's. Encoding the whole path is what
        makes any stored DOI safe to send; the round trip below is what proves
        the encoded path still denotes the DOI that was asked for.
        """
        doi = "10.1016/s0140-6736(03)14065-2"
        client = _client({doi: None})
        assert DataCite(client).by_dois([doi]) == {}
        path = client.urls[0][len(_BASE) :]
        assert not set(path) & set("()#? <>\"")
        assert urllib.parse.unquote(path) == doi

    def test_an_unusable_doi_never_reaches_the_registry(self) -> None:
        """Entries with an empty ``doi`` field are common in exported ``.bib``."""
        client = _client({})
        assert DataCite(client).by_dois(["", "   "]) == {}
        assert client.urls == []
