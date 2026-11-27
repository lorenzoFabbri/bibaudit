"""zbMATH Open: a byline for a DOI, and nothing where it says nothing about one."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from bibaudit.registries.http import Transient
from bibaudit.registries.zbmath import ZbMath, search_url

DATA = Path(__file__).parent / "data"
BARNARD = "10.1093/biomet/86.4.948"


def _recorded(fixture: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((DATA / fixture).read_text(encoding="utf-8"))
    return payload


class _Client:
    def __init__(self, payload: dict[str, Any] | None, *, transient: bool = False) -> None:
        self.payload = payload
        self.transient = transient
        self.urls: list[str] = []

    def get_json(self, url: str) -> dict[str, Any] | None:
        self.urls.append(url)
        if self.transient:
            raise Transient("simulated outage")
        return self.payload


def _by_doi(payload: dict[str, Any] | None, doi: str = BARNARD) -> Any:
    return ZbMath(_Client(payload)).by_doi(doi)  # type: ignore[arg-type]


class TestTheByline:
    def test_every_named_author_is_read(self) -> None:
        record = _by_doi(_recorded("zbmath_byline_past_crossref.json"))

        assert record.source == "zbmath"
        assert record.doi == BARNARD
        assert [(n.family, n.given) for n in record.authors] == [
            ("Barnard", "John"),
            ("Rubin", "Donald B."),
        ]

    def test_nothing_but_the_byline_is_carried(self) -> None:
        record = _by_doi(_recorded("zbmath_byline_past_crossref.json"))

        assert (record.title, record.container, record.years, record.retracted) == (
            None, None, {}, False,
        )

    def test_a_record_whose_every_name_is_withheld_has_no_byline(self) -> None:
        payload = _recorded("zbmath_byline_withheld.json")

        assert _by_doi(payload, "10.1080/01621459.2017.1422737") is None

    def test_a_withheld_name_beside_a_named_one_is_dropped(self) -> None:
        payload = deepcopy(_recorded("zbmath_byline_past_crossref.json"))
        withheld = _recorded("zbmath_byline_withheld.json")
        payload["result"][0]["contributors"]["authors"][1]["name"] = (
            withheld["result"][0]["contributors"]["authors"][0]["name"]
        )

        assert [n.family for n in _by_doi(payload).authors] == ["Barnard"]


class TestWhereZbMathSaysNothing:
    def test_a_404_is_no_byline(self) -> None:
        assert _by_doi(None) is None

    def test_a_result_linking_another_doi_is_no_byline(self) -> None:
        assert _by_doi(_recorded("zbmath_byline_past_crossref.json"), "10.1093/biomet/70.1.41") is None

    def test_an_outage_is_not_absence(self) -> None:
        with pytest.raises(Transient):
            ZbMath(_Client(None, transient=True)).by_doi(BARNARD)  # type: ignore[arg-type]


class TestTheRequest:
    def test_the_doi_is_asked_as_an_external_identifier(self) -> None:
        """``doi:`` misses a DOI with parentheses that ``en:`` finds."""
        client = _Client(None)
        ZbMath(client).by_doi("10.1016/S0019-9958(65)90241-X")  # type: ignore[arg-type]

        assert client.urls == [search_url("10.1016/s0019-9958(65)90241-x")]
        assert "search_string=en%3A10.1016%2Fs0019-9958%2865%2990241-x" in client.urls[0]

    def test_a_doi_is_matched_in_any_case(self) -> None:
        assert _by_doi(_recorded("zbmath_byline_past_crossref.json"), BARNARD.upper()) is not None
