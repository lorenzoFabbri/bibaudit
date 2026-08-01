"""Reading a Zotero library: the live ``zotero.sqlite``, a CSL-JSON export,
Zotero's own item JSON, and the desktop client's local HTTP API.

Everything here is offline. The sqlite tests build a real database from the
schema subset in :data:`_ZOTERO_SCHEMA` rather than shipping a binary fixture,
because the point of most of them is *which table a column is resolved
against* — something a checked-in ``.sqlite`` would hide behind an opaque blob
that nobody would ever open to check.

Two properties of the fixture database are deliberate traps and must not be
"tidied up":

* ``fields`` and ``fieldsCombined`` are given **different** id -> name
  mappings. Zotero's ``itemData.fieldID`` foreign key points at
  ``fieldsCombined``; a reader that resolves against ``fields`` therefore pulls
  a value out of the wrong column, and here that turns a call number into the
  DOI. Making the two tables agree would delete the only evidence the right
  table is being read.
* There is a second, *group* library holding an item whose Zotero ``key`` is
  identical to a personal-library item's. Keys are unique only within a
  library, so an unscoped read merges a synced group library into "My Library"
  under colliding keys — the regression these tests exist to catch.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import shutil
import sqlite3
import tempfile
import types
import urllib.error
import urllib.request
import warnings
from typing import Any

import pytest

from bibaudit.adapters import zotero
from bibaudit.adapters.zotero import default_zotero_paths, read_csl_json, read_zotero
from bibaudit.model import Reference

_DATA = pathlib.Path(__file__).parent / "data"

#: The fixture database deliberately holds group libraries, so almost every
#: sqlite test here trips the "group libraries were not read" notice. Silenced
#: at module level so it does not bury the suite's real output under one
#: identical line per test — never globally, and never in the tests that assert
#: it: ``pytest.warns`` installs its own filters, so
#: :class:`TestGroupLibrariesAreAnnounced` still sees every one.
pytestmark = pytest.mark.filterwarnings(
    "ignore:this Zotero database also holds:RuntimeWarning"
)

# --------------------------------------------------------------------------
# The schema subset, copied from a real Zotero 7 database
# (``version.userdata`` = 125, ``version.system`` = 32, ``version.compatibility``
# = 7). Column names, NOT NULL constraints and — most importantly —
# ``itemData``'s foreign key onto ``fieldsCombined`` are reproduced verbatim;
# only tables this adapter never touches are omitted.
# --------------------------------------------------------------------------

_ZOTERO_SCHEMA = """
CREATE TABLE libraries (
    libraryID INTEGER PRIMARY KEY,
    type TEXT NOT NULL,
    editable INT NOT NULL,
    filesEditable INT NOT NULL,
    version INT NOT NULL DEFAULT 0,
    storageVersion INT NOT NULL DEFAULT 0,
    lastSync INT NOT NULL DEFAULT 0,
    archived INT NOT NULL DEFAULT 0,
    isAdmin INT NOT NULL DEFAULT 0
);
CREATE TABLE itemTypes (
    itemTypeID INTEGER PRIMARY KEY,
    typeName TEXT,
    templateItemTypeID INT,
    display INT DEFAULT 1
);
CREATE TABLE items (
    itemID INTEGER PRIMARY KEY,
    itemTypeID INT NOT NULL,
    dateAdded TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    dateModified TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    clientDateModified TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    libraryID INT NOT NULL,
    key TEXT NOT NULL,
    version INT NOT NULL DEFAULT 0,
    synced INT NOT NULL DEFAULT 0,
    UNIQUE (libraryID, key),
    FOREIGN KEY (libraryID) REFERENCES libraries(libraryID) ON DELETE CASCADE
);
CREATE TABLE fields (
    fieldID INTEGER PRIMARY KEY,
    fieldName TEXT,
    fieldFormatID INT
);
CREATE TABLE fieldsCombined (
    fieldID INT NOT NULL,
    fieldName TEXT NOT NULL,
    label TEXT,
    fieldFormatID INT,
    custom INT NOT NULL,
    PRIMARY KEY (fieldID)
);
CREATE TABLE itemDataValues (
    valueID INTEGER PRIMARY KEY,
    value UNIQUE
);
CREATE TABLE itemData (
    itemID INT,
    fieldID INT,
    valueID,
    PRIMARY KEY (itemID, fieldID),
    FOREIGN KEY (itemID) REFERENCES items(itemID) ON DELETE CASCADE,
    FOREIGN KEY (fieldID) REFERENCES fieldsCombined(fieldID),
    FOREIGN KEY (valueID) REFERENCES itemDataValues(valueID)
);
CREATE TABLE creatorTypes (
    creatorTypeID INTEGER PRIMARY KEY,
    creatorType TEXT
);
CREATE TABLE creators (
    creatorID INTEGER PRIMARY KEY,
    firstName TEXT,
    lastName TEXT,
    fieldMode INT,
    UNIQUE (lastName, firstName, fieldMode)
);
CREATE TABLE itemCreators (
    itemID INT NOT NULL,
    creatorID INT NOT NULL,
    creatorTypeID INT NOT NULL DEFAULT 1,
    orderIndex INT NOT NULL DEFAULT 0,
    PRIMARY KEY (itemID, creatorID, creatorTypeID, orderIndex),
    UNIQUE (itemID, orderIndex),
    FOREIGN KEY (itemID) REFERENCES items(itemID) ON DELETE CASCADE,
    FOREIGN KEY (creatorID) REFERENCES creators(creatorID) ON DELETE CASCADE,
    FOREIGN KEY (creatorTypeID) REFERENCES creatorTypes(creatorTypeID)
);
CREATE TABLE collections (
    collectionID INTEGER PRIMARY KEY,
    collectionName TEXT NOT NULL,
    parentCollectionID INT DEFAULT NULL,
    clientDateModified TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    libraryID INT NOT NULL,
    key TEXT NOT NULL,
    version INT NOT NULL DEFAULT 0,
    synced INT NOT NULL DEFAULT 0,
    UNIQUE (libraryID, key),
    FOREIGN KEY (libraryID) REFERENCES libraries(libraryID) ON DELETE CASCADE,
    FOREIGN KEY (parentCollectionID) REFERENCES collections(collectionID) ON DELETE CASCADE
);
CREATE TABLE collectionItems (
    collectionID INT NOT NULL,
    itemID INT NOT NULL,
    orderIndex INT NOT NULL DEFAULT 0,
    PRIMARY KEY (collectionID, itemID),
    FOREIGN KEY (collectionID) REFERENCES collections(collectionID) ON DELETE CASCADE,
    FOREIGN KEY (itemID) REFERENCES items(itemID) ON DELETE CASCADE
);
CREATE TABLE deletedItems (
    itemID INTEGER PRIMARY KEY,
    dateDeleted DEFAULT CURRENT_TIMESTAMP NOT NULL,
    FOREIGN KEY (itemID) REFERENCES items(itemID) ON DELETE CASCADE
);
CREATE TABLE groups (
    groupID INTEGER PRIMARY KEY,
    libraryID INT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    version INT NOT NULL,
    FOREIGN KEY (libraryID) REFERENCES libraries(libraryID) ON DELETE CASCADE
);
"""

#: itemTypeIDs as a Zotero 7 install numbers them. Hard-coded rather than
#: allocated sequentially so the fixture is recognisably the real thing; the
#: adapter must resolve every one of them by *name* regardless.
_TYPE_IDS = {
    "annotation": 1,
    "attachment": 3,
    "book": 7,
    "bookSection": 8,
    "journalArticle": 22,
    "note": 28,
    # The one id below that is not copied from a real install. It exists so the
    # fixture can hold a *feed* library with something in it (see library 4 in
    # _build_library): feeds are subscriptions, not citable works, and the
    # group-library notice must not name one. Every id here is resolved by
    # name, so the number itself is never asserted on.
    "feedItem": 39,
}

_CREATOR_TYPE_IDS = {"author": 8, "editor": 10, "translator": 11}

#: The authoritative id -> name mapping, i.e. what ``fieldsCombined`` holds and
#: what ``itemData.fieldID`` actually means. The numbers are the real ones from
#: a Zotero 7 database; ``customPluginField`` stands in for a field a
#: translator or plugin registered, which by construction exists only here.
_FIELD_IDS = {
    "title": 1,
    "date": 6,
    "url": 13,
    "volume": 19,
    "publisher": 23,
    "ISBN": 25,
    "callNumber": 26,
    "pages": 32,
    "publicationTitle": 38,
    "bookTitle": 45,
    "DOI": 59,
    "issue": 76,
    "customPluginField": 10000,
}

_CUSTOM_FIELD_NAME = "customPluginField"


def _skewed_fields_rows() -> list[tuple[int, str]]:
    """What the *wrong* table, ``fields``, says in this fixture.

    ``DOI`` and ``callNumber`` are swapped relative to ``fieldsCombined``, and
    the plugin-registered field is absent. A reader that resolves
    ``itemData.fieldID`` here instead of against ``fieldsCombined`` therefore
    reports the article's call number as its DOI — a visible, assertable wrong
    answer rather than a silently missing one.
    """
    swapped = dict(_FIELD_IDS)
    swapped["DOI"] = _FIELD_IDS["callNumber"]
    swapped["callNumber"] = _FIELD_IDS["DOI"]
    return [(fid, name) for name, fid in swapped.items() if name != _CUSTOM_FIELD_NAME]


def _correct_fields_rows() -> list[tuple[int, str]]:
    """``fields`` as a pre-5.0.71 database (no ``fieldsCombined``) would hold it.

    Such a database predates custom fields entirely, so the plugin field is
    absent here too — that is the schema era, not an omission.
    """
    return [(fid, name) for name, fid in _FIELD_IDS.items() if name != _CUSTOM_FIELD_NAME]


def _build_library(
    path: pathlib.Path, *, legacy_schema: bool = False, wal: bool = False
) -> None:
    """Write a small but faithful ``zotero.sqlite`` to *path*.

    With *legacy_schema* the database is built as a Zotero older than 5.0.71:
    no ``fieldsCombined`` table at all, and a correct ``fields`` table, which
    is the situation the adapter's fallback exists for.

    With *wal* the file is left in write-ahead-log mode. The real Zotero 7
    database is in legacy-journal mode (its header bytes 18/19 are ``01 01``),
    but WAL is the mode in which sqlite has to build a ``-shm`` index beside
    the file in order to read it — which is what makes "bibaudit created no
    file in the user's Zotero directory" an observable property rather than an
    assertion about a connection string. See
    :meth:`TestReadOnly.test_reading_a_wal_mode_library_creates_no_sidecar_files`.
    """
    conn = sqlite3.connect(path)
    try:
        if wal:
            conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(_ZOTERO_SCHEMA)

        # -- lookup tables -------------------------------------------------
        conn.executemany(
            "INSERT INTO itemTypes (itemTypeID, typeName) VALUES (?, ?)",
            [(type_id, name) for name, type_id in _TYPE_IDS.items()],
        )
        conn.executemany(
            "INSERT INTO creatorTypes (creatorTypeID, creatorType) VALUES (?, ?)",
            [(type_id, name) for name, type_id in _CREATOR_TYPE_IDS.items()],
        )
        if legacy_schema:
            conn.execute("DROP TABLE fieldsCombined")
            conn.executemany(
                "INSERT INTO fields (fieldID, fieldName) VALUES (?, ?)", _correct_fields_rows()
            )
        else:
            conn.executemany(
                "INSERT INTO fields (fieldID, fieldName) VALUES (?, ?)", _skewed_fields_rows()
            )
            conn.executemany(
                "INSERT INTO fieldsCombined (fieldID, fieldName, custom) VALUES (?, ?, ?)",
                [
                    (fid, name, 1 if name == _CUSTOM_FIELD_NAME else 0)
                    for name, fid in _FIELD_IDS.items()
                ],
            )

        # -- libraries: one personal, two synced groups, one feed -----------
        # Library 3 and library 4 exist only for the group-library notice: a
        # group holding nothing citable and a feed subscription must both stay
        # unnamed, or the notice becomes the noise it is meant to prevent.
        conn.executemany(
            "INSERT INTO libraries (libraryID, type, editable, filesEditable) VALUES (?, ?, 1, 1)",
            [(1, "user"), (2, "group"), (3, "group"), (4, "feed")],
        )
        # Only group libraries have a `groups` row; a feed library has none,
        # which is why _group_libraries LEFT JOINs rather than filtering here.
        conn.executemany(
            "INSERT INTO groups (groupID, libraryID, name, description, version) "
            "VALUES (?, ?, ?, '', 1)",
            [(1, 2, "MCC-Spain Reading Group"), (2, 3, "Slides and Handouts")],
        )

        value_ids: dict[str, int] = {}

        def value_id(value: str) -> int:
            if value not in value_ids:
                value_ids[value] = len(value_ids) + 1
                conn.execute(
                    "INSERT INTO itemDataValues (valueID, value) VALUES (?, ?)",
                    (value_ids[value], value),
                )
            return value_ids[value]

        def add_item(
            item_id: int,
            key: str,
            type_name: str,
            fields: dict[str, str],
            *,
            library_id: int = 1,
        ) -> None:
            conn.execute(
                "INSERT INTO items (itemID, itemTypeID, libraryID, key) VALUES (?, ?, ?, ?)",
                (item_id, _TYPE_IDS[type_name], library_id, key),
            )
            for name, value in fields.items():
                conn.execute(
                    "INSERT INTO itemData (itemID, fieldID, valueID) VALUES (?, ?, ?)",
                    (item_id, _FIELD_IDS[name], value_id(value)),
                )

        def add_creator(
            creator_id: int, last: str, first: str = "", field_mode: int = 0
        ) -> None:
            conn.execute(
                "INSERT INTO creators (creatorID, firstName, lastName, fieldMode) VALUES (?, ?, ?, ?)",
                (creator_id, first, last, field_mode),
            )

        def link_creator(item_id: int, creator_id: int, role: str, order_index: int) -> None:
            conn.execute(
                "INSERT INTO itemCreators (itemID, creatorID, creatorTypeID, orderIndex) "
                "VALUES (?, ?, ?, ?)",
                (item_id, creator_id, _CREATOR_TYPE_IDS[role], order_index),
            )

        # -- item 1: an ordinary journal article, the round-trip case ------
        # Its `date` uses Zotero's own stored form, "<SQL date> <as typed>",
        # which is what the column actually holds -- an adapter that did
        # int(value) on it would raise, and one that took the first token
        # would still have to cope with the "2018-00-00" month/day zeros used
        # for a year-only date (item 4 below).
        add_item(
            1,
            "ARTICLE01",
            "journalArticle",
            {
                "title": "Shift work and colorectal cancer risk in the MCC-Spain case-control study",
                "publicationTitle": "American Journal of Epidemiology",
                "date": "2017-08-15 2017-08-15",
                "DOI": "10.1093/aje/kwx137",
                "volume": "186",
                "issue": "5",
                "pages": "533-540",
                "url": "https://doi.org/10.1093/aje/kwx137",
                # Stored under the fieldID that the skewed `fields` table calls
                # "DOI"; see _skewed_fields_rows.
                "callNumber": "RA645.C3",
                "customPluginField": "papantoniou2017",
            },
        )
        add_creator(1, "Papantoniou", "Kyriaki")
        add_creator(2, "Castano-Vinyals", "Gemma")
        link_creator(1, 1, "author", 0)
        link_creator(1, 2, "author", 1)

        # -- item 2: a single-field (fieldMode 1) corporate byline ----------
        add_item(
            2,
            "COLLECT01",
            "journalArticle",
            {
                "title": "Endogenous sex hormones and breast cancer in postmenopausal women",
                "publicationTitle": "JNCI: Journal of the National Cancer Institute",
                "date": "2002-04-17 2002-04-17",
                "DOI": "10.1093/jnci/94.8.606",
            },
        )
        add_creator(3, "The Endogenous Hormones and Breast Cancer Collaborative Group", "", 1)
        link_creator(2, 3, "author", 0)

        # -- item 3: creators whose byline order is none of the orders a
        # missing "ORDER BY orderIndex" would produce. Insertion (rowid) order
        # is Aragones, Malats, Kogevinas; creatorID order is Malats,
        # Kogevinas, Aragones; the correct byline is none of those.
        add_item(
            3,
            "ORDERIDX1",
            "journalArticle",
            {
                "title": "Cohort profile: the MCC-Spain study",
                "publicationTitle": "International Journal of Epidemiology",
                "date": "2015-00-00 2015",
            },
        )
        add_creator(10, "Malats", "Nuria")
        add_creator(20, "Kogevinas", "Manolis")
        add_creator(30, "Aragones", "Nuria")
        link_creator(3, 30, "author", 1)
        link_creator(3, 10, "author", 2)
        link_creator(3, 20, "author", 0)

        # -- item 4: an edited volume with no author-typed creator at all ---
        add_item(
            4,
            "EDITEDVOL",
            "book",
            {
                "title": "Cancer Epidemiology and Prevention",
                "publisher": "Oxford University Press",
                "ISBN": "9780190238667",
                "date": "2018-00-00 2018",
            },
        )
        add_creator(40, "Thun", "Michael")
        add_creator(41, "Linet", "Martha")
        link_creator(4, 40, "editor", 0)
        link_creator(4, 41, "editor", 1)

        # -- item 5: an editor listed *before* the author, so taking
        # creators[0] rather than filtering on creatorType gets it wrong.
        add_item(
            5,
            "BOTHROLES",
            "journalArticle",
            {
                "title": "Commentary on night shift work and cancer",
                "publicationTitle": "Occupational and Environmental Medicine",
                "date": "2019-03-01 2019-03-01",
            },
        )
        add_creator(50, "Boffetta", "Paolo")
        add_creator(51, "Straif", "Kurt")
        link_creator(5, 50, "editor", 0)
        link_creator(5, 51, "author", 1)

        # -- item 6: in the trash -----------------------------------------
        add_item(
            6,
            "TRASHED01",
            "journalArticle",
            {"title": "A duplicate the user moved to the trash", "date": "2011-00-00 2011"},
        )
        conn.execute("INSERT INTO deletedItems (itemID) VALUES (6)")

        # -- items 7-9: not citable works in their own right ---------------
        add_item(7, "ATTACH001", "attachment", {"title": "Full Text PDF"})
        add_item(8, "NOTE00001", "note", {"title": "Reading note"})
        add_item(9, "ANNOT0001", "annotation", {"title": "Highlighted passage"})

        # -- item 10: only in a *child* collection -------------------------
        # Its DOI carries the parentheses that Lancet and Elsevier DOIs have;
        # a hand-rolled DOI regex truncates it at the bracket.
        add_item(
            10,
            "CHILDCOLL",
            "journalArticle",
            {
                "title": "Efficacy and safety of cholesterol-lowering treatment",
                "publicationTitle": "The Lancet",
                "DOI": "10.1016/S0140-6736(03)14065-2",
                "date": "2005-01-14 2005-01-14",
            },
        )

        # -- item 11: in an unrelated collection ---------------------------
        add_item(
            11,
            "OTHERCOLL",
            "journalArticle",
            # "0000-00-00 n.d." is the multipart value Zotero writes when the
            # typed date holds no parseable year at all. It is not a typo in
            # the fixture: it is the case that separates "no year recorded"
            # from "year 0".
            {"title": "An item filed under Methods only", "date": "0000-00-00 n.d."},
        )

        # -- item 12: a book section, whose container is bookTitle ---------
        add_item(
            12,
            "BOOKSECT1",
            "bookSection",
            {
                "title": "Nutritional epidemiology",
                "bookTitle": "Cancer Epidemiology and Prevention",
                "publisher": "Oxford University Press",
                "date": "2018-00-00 2018",
            },
        )
        add_creator(60, "Willett", "Walter")
        link_creator(12, 60, "author", 0)

        # -- item 13: two levels down the collection tree ------------------
        # Filed under Epidemiology > Shift work > Night work. A traversal that
        # takes one step from the named collection reaches item 10 and stops,
        # which looks right until a user with a three-level project folder
        # gets a subset of their bibliography checked and no warning.
        add_item(
            13,
            "GRANDKID1",
            "journalArticle",
            {
                "title": "Night shift work and breast cancer incidence",
                "publicationTitle": "Scandinavian Journal of Work, Environment & Health",
                "date": "2013-00-00 2013",
            },
        )

        # -- library 2: a synced group library ------------------------------
        # Item 100 reuses key "ARTICLE01": Zotero keys are unique per library,
        # so this is legal and is exactly the collision an unscoped read hits.
        add_item(
            100,
            "ARTICLE01",
            "journalArticle",
            {
                "title": "A group library article that must never be read",
                "date": "2021-00-00 2021",
            },
            library_id=2,
        )
        add_item(
            101,
            "GROUPART2",
            "journalArticle",
            {"title": "Another group library article", "date": "2022-00-00 2022"},
            library_id=2,
        )
        add_creator(70, "Phantom", "Group")
        link_creator(100, 70, "author", 0)
        # Trashed inside the group library. The notice counts what the user
        # *would* have got, on the same terms the personal library is read on,
        # so this one is not counted unless include_trashed says so.
        add_item(
            102,
            "GROUPTRSH",
            "journalArticle",
            {"title": "A group library article in the group's trash", "date": "2023-00-00 2023"},
            library_id=2,
        )
        conn.execute("INSERT INTO deletedItems (itemID) VALUES (102)")

        # -- library 3: a group holding nothing citable ---------------------
        # A user belongs to plenty of groups that only ever hold slide decks.
        # Naming this one would be a false alarm in the one message whose only
        # job is to be believed.
        add_item(103, "GRPATTACH", "attachment", {"title": "Workshop slides.pdf"}, library_id=3)

        # -- library 4: an RSS feed ----------------------------------------
        # `libraries.type` also takes 'feed'. Scoping the notice to
        # `type = 'group'` rather than `type != 'user'` is what keeps a journal
        # table-of-contents subscription out of it.
        add_item(
            104,
            "FEEDITEM1",
            "feedItem",
            {"title": "New issue: American Journal of Epidemiology"},
            library_id=4,
        )

        # -- collections ---------------------------------------------------
        conn.executemany(
            "INSERT INTO collections (collectionID, collectionName, parentCollectionID, libraryID, key) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (1, "Epidemiology", None, 1, "COLLKEY01"),
                (2, "Shift work", 1, 1, "COLLKEY02"),
                (3, "Methods", None, 1, "COLLKEY03"),
                # Same display name as collection 1, but in the group library.
                (4, "Epidemiology", None, 2, "COLLKEY04"),
                (5, "Group Only Reading List", None, 2, "COLLKEY05"),
                # A grandchild of Epidemiology: 1 -> 2 -> 6.
                (6, "Night work", 2, 1, "COLLKEY06"),
            ],
        )
        conn.executemany(
            "INSERT INTO collectionItems (collectionID, itemID) VALUES (?, ?)",
            [
                (1, 1),
                (2, 10),
                (3, 11),
                # The trashed item is filed in Methods on purpose: without it,
                # "a trashed item inside the collection is still excluded"
                # asserts nothing, because no trashed item would be in range
                # of the collection filter in the first place.
                (3, 6),
                (4, 101),
                (5, 101),
                (6, 13),
            ],
        )

        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def library(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """A built ``zotero.sqlite``, shared by the tests that only read it."""
    path = tmp_path_factory.mktemp("zotero-library") / "zotero.sqlite"
    _build_library(path)
    return path


@pytest.fixture(scope="module")
def refs(library: pathlib.Path) -> list[Reference]:
    return read_zotero(library)


@pytest.fixture(scope="module")
def by_key(refs: list[Reference]) -> dict[str, Reference]:
    return {ref.key: ref for ref in refs}


def _snapshot_path_in(caught: pytest.WarningsRecorder) -> pathlib.Path:
    """The temp copy named in the module's mid-write warning.

    Read out of the warning text rather than out of ``tempfile.gettempdir()``
    so the test cleans up — and asserts on — the very file the user was told
    about, not merely some file with a matching prefix.

    Searches every recorded warning instead of taking ``caught[0]``: reading a
    library that also holds group libraries emits a second, unrelated
    RuntimeWarning, and an index would silently start asserting against
    whichever warning happened to be raised first.
    """
    for entry in caught:
        match = re.search(
            r"point-in-time copy at (.+?) instead of the live database", str(entry.message)
        )
        if match is not None:
            return pathlib.Path(match.group(1))
    raise AssertionError(
        f"no snapshot warning among {[str(entry.message) for entry in caught]}"
    )


def _opened_database_recorder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record which file each ``_read_sqlite`` connection actually opened.

    Wraps the module's own ``_extract_references`` so the connection under
    test is the one the module built, not one the test reconstructed — a test
    that rebuilds the URI itself would keep passing if ``_read_sqlite`` stopped
    using ``_sqlite_uri``.
    """
    opened: list[str] = []
    real = zotero._extract_references

    def spy(conn: sqlite3.Connection, **kwargs: Any) -> Any:
        opened.append(conn.execute("PRAGMA database_list").fetchone()["file"])
        return real(conn, **kwargs)

    monkeypatch.setattr(zotero, "_extract_references", spy)
    return opened


class TestSqliteRoundTrip:
    """One ordinary journal article, read out of the real schema."""

    def test_scalar_fields_round_trip(self, by_key: dict[str, Reference]) -> None:
        ref = by_key["ARTICLE01"]
        assert ref.title == (
            "Shift work and colorectal cancer risk in the MCC-Spain case-control study"
        )
        assert ref.container == "American Journal of Epidemiology"
        assert ref.year == 2017
        assert ref.doi == "10.1093/aje/kwx137"
        assert ref.volume == "186"
        assert ref.issue == "5"
        assert ref.pages == "533-540"
        assert ref.url == "https://doi.org/10.1093/aje/kwx137"

    def test_key_locator_and_kind(self, by_key: dict[str, Reference]) -> None:
        ref = by_key["ARTICLE01"]
        assert ref.locator == "zotero:ARTICLE01"
        assert ref.kind == "article"
        # The untouched Zotero type is kept for the report and for --suggest;
        # normalize_kind() is lossy (journalArticle and inproceedings both
        # become "article") and the original cannot be recovered from it.
        assert ref.raw["itemType"] == "journalArticle"

    def test_zoteros_two_part_date_column_yields_the_year(
        self, by_key: dict[str, Reference]
    ) -> None:
        """The ``date`` column holds "2015-00-00 2015", not "2015".

        Zotero stores a normalised SQL date followed by the string the user
        actually typed, with ``00`` for the parts they left out. ``int()`` on
        that value raises, so the year has to be extracted rather than cast.
        """
        assert by_key["ORDERIDX1"].year == 2015
        assert by_key["EDITEDVOL"].year == 2018

    def test_a_date_with_no_parseable_year_is_none_not_zero(
        self, by_key: dict[str, Reference]
    ) -> None:
        """Zotero writes "0000-00-00 n.d." for an undated item.

        Reading the leading four characters as an integer yields year 0, which
        compare.py then checks against the registry and reports as a year
        mismatch on every undated item in the library. ``None`` means "the
        library does not record a year" and is skipped instead.
        """
        assert by_key["OTHERCOLL"].year is None

    def test_parenthesised_doi_is_not_truncated(self, by_key: dict[str, Reference]) -> None:
        """Lancet and Elsevier DOIs contain brackets; see normalize.DOI_PATTERN."""
        assert by_key["CHILDCOLL"].doi == "10.1016/s0140-6736(03)14065-2"

    def test_book_section_takes_its_container_from_booktitle(
        self, by_key: dict[str, Reference]
    ) -> None:
        """A chapter has no publicationTitle; without the bookTitle fallback its
        container is empty and every container check on it becomes vacuous.
        """
        ref = by_key["BOOKSECT1"]
        assert ref.kind == "chapter"
        assert ref.container == "Cancer Epidemiology and Prevention"

    def test_book_carries_isbn_and_publisher(self, by_key: dict[str, Reference]) -> None:
        ref = by_key["EDITEDVOL"]
        assert ref.kind == "book"
        assert ref.isbn == "9780190238667"
        assert ref.publisher == "Oxford University Press"

    def test_absent_fields_are_none_not_empty_string(
        self, by_key: dict[str, Reference]
    ) -> None:
        """`None` means "the library does not record this"; "" would be read
        downstream as "the library records an empty value", which compare.py
        would then have to treat as a mismatch against a registry that has one.
        """
        ref = by_key["OTHERCOLL"]
        assert ref.doi is None
        assert ref.volume is None
        assert ref.pages is None
        assert ref.container is None


class TestFieldResolution:
    """``itemData.fieldID`` points at ``fieldsCombined``, not ``fields``."""

    def test_ids_are_resolved_against_fieldscombined(
        self, by_key: dict[str, Reference]
    ) -> None:
        """The fixture's two field tables disagree on purpose.

        ``fields`` calls fieldID 26 "DOI" and fieldID 59 "callNumber";
        ``fieldsCombined`` — the table Zotero's own foreign key targets — has
        it the other way round. A reader that consults ``fields`` therefore
        reports the call number "RA645.C3" as this article's DOI.
        """
        ref = by_key["ARTICLE01"]
        assert ref.doi == "10.1093/aje/kwx137"
        assert ref.raw["fields"]["callNumber"] == "RA645.C3"

    def test_a_plugin_registered_field_is_still_read(
        self, by_key: dict[str, Reference]
    ) -> None:
        """``fields`` omits custom fields entirely, so a value stored under one
        vanishes from ``raw`` with no error at all if the wrong table is read —
        the silent half of the same bug.
        """
        assert by_key["ARTICLE01"].raw["fields"]["customPluginField"] == "papantoniou2017"

    def test_a_database_predating_fieldscombined_falls_back_to_fields(
        self, tmp_path: pathlib.Path
    ) -> None:
        """``fieldsCombined`` arrived in Zotero 5.0.71 (2019).

        An older database has no custom fields to lose, so falling back to
        ``fields`` there must keep working rather than raising — a library
        synced since 2015 is precisely this tool's audience.
        """
        path = tmp_path / "zotero.sqlite"
        _build_library(path, legacy_schema=True)
        refs = {ref.key: ref for ref in read_zotero(path)}
        assert refs["ARTICLE01"].doi == "10.1093/aje/kwx137"
        assert "customPluginField" not in refs["ARTICLE01"].raw["fields"]


class TestCreators:
    def test_field_mode_one_creator_is_one_collective_name(
        self, by_key: dict[str, Reference]
    ) -> None:
        """"The Endogenous Hormones and Breast Cancer Collaborative Group" is a
        single corporate author. Zotero records it with fieldMode 1 and the
        whole string in lastName; reading it as a family name produces a
        surname that no registry will ever match.
        """
        authors = by_key["COLLECT01"].authors
        assert len(authors) == 1
        assert authors[0].collective
        assert authors[0].literal == (
            "The Endogenous Hormones and Breast Cancer Collaborative Group"
        )
        assert authors[0].family == ""
        assert authors[0].given == ""

    def test_ordinary_creator_splits_into_family_and_given(
        self, by_key: dict[str, Reference]
    ) -> None:
        authors = by_key["ARTICLE01"].authors
        assert [(a.family, a.given) for a in authors] == [
            ("Papantoniou", "Kyriaki"),
            ("Castano-Vinyals", "Gemma"),
        ]
        assert not any(a.collective for a in authors)

    def test_creators_come_back_in_order_index_order(
        self, by_key: dict[str, Reference]
    ) -> None:
        """Byline order is orderIndex, and nothing else.

        The fixture's rows were inserted in one order and given creatorIDs in
        another, both different from orderIndex, so a query that lost its
        ORDER BY cannot land on the right answer by luck. Author order is
        compared position by position against the registry; a permuted list
        reports every author as mismatched.
        """
        assert [a.family for a in by_key["ORDERIDX1"].authors] == [
            "Kogevinas",
            "Aragones",
            "Malats",
        ]

    def test_editor_substitutes_only_when_there_is_no_author(
        self, by_key: dict[str, Reference]
    ) -> None:
        """An edited volume cited by its editors is correct, not author-less."""
        ref = by_key["EDITEDVOL"]
        assert [a.family for a in ref.authors] == ["Thun", "Linet"]
        assert ref.raw["creator_role"] == "editor"

    def test_author_wins_over_an_editor_listed_first(
        self, by_key: dict[str, Reference]
    ) -> None:
        """The editor has the lower orderIndex here on purpose: selecting on
        creatorType, not position, is what makes the right creator win.
        """
        ref = by_key["BOTHROLES"]
        assert [a.family for a in ref.authors] == ["Straif"]
        assert "creator_role" not in ref.raw

    def test_raw_keeps_every_creator_including_the_unused_roles(
        self, by_key: dict[str, Reference]
    ) -> None:
        """`authors` is the compared byline; `raw["creators"]` is the record of
        what the library actually holds, which the report and --suggest need.
        """
        roles = [c["creatorType"] for c in by_key["BOTHROLES"].raw["creators"]]
        assert roles == ["editor", "author"]


class TestItemSelection:
    def test_attachments_notes_and_annotations_are_excluded(
        self, by_key: dict[str, Reference]
    ) -> None:
        """One article with a PDF, a note and a highlight is one citation.

        Reading them in makes a library of 40 papers report as 160 references,
        most of them "incomplete".
        """
        assert {"ATTACH001", "NOTE00001", "ANNOT0001"}.isdisjoint(by_key)

    def test_trashed_items_are_excluded_by_default(
        self, by_key: dict[str, Reference]
    ) -> None:
        assert "TRASHED01" not in by_key

    def test_trashed_items_appear_with_include_trashed(
        self, library: pathlib.Path
    ) -> None:
        keys = {ref.key for ref in read_zotero(library, include_trashed=True)}
        assert "TRASHED01" in keys
        # The trash is added to, not substituted for, the live library.
        assert "ARTICLE01" in keys

    def test_the_live_library_is_exactly_the_expected_items(
        self, by_key: dict[str, Reference]
    ) -> None:
        """Pinned so a filter that silently stops excluding something is caught
        even if no other test happens to look at that item.
        """
        assert set(by_key) == {
            "ARTICLE01",
            "COLLECT01",
            "ORDERIDX1",
            "EDITEDVOL",
            "BOTHROLES",
            "CHILDCOLL",
            "OTHERCOLL",
            "BOOKSECT1",
            "GRANDKID1",
        }


class TestLibraryScoping:
    """Only the personal library is read. Group libraries are somebody else's."""

    def test_group_library_items_are_not_returned(
        self, by_key: dict[str, Reference]
    ) -> None:
        """The sqlite path used to read every library in the file.

        A synced group library shares the ``items`` table with "My Library",
        so an unscoped query pulls in hundreds of works the user never cited
        and reports each of them.
        """
        assert "GROUPART2" not in by_key
        titles = {ref.title for ref in by_key.values()}
        assert "A group library article that must never be read" not in titles
        assert "Another group library article" not in titles

    def test_a_key_reused_across_libraries_resolves_to_the_personal_item(
        self, refs: list[Reference]
    ) -> None:
        """Zotero keys are unique only *within* a library.

        Both the personal item 1 and the group item 100 have key ARTICLE01.
        An unscoped read returns two References under that one key, and
        whichever the caller's ``{ref.key: ref}`` dict keeps last wins —
        silently substituting a group library's work for the user's own.

        Asserted on the list rather than on ``by_key``: the dict has already
        thrown the duplicate away, so a count there is not observable, and
        which of the two survives is left to sqlite's row order.
        """
        matching = [ref for ref in refs if ref.key == "ARTICLE01"]
        assert len(matching) == 1
        title = matching[0].title
        assert title is not None and title.startswith("Shift work")

    def test_a_collection_that_exists_only_in_a_group_library_is_not_found(
        self, library: pathlib.Path
    ) -> None:
        """Collection *names* are per-library too.

        Resolving the name across every library would make this return an
        empty list — "your collection is empty" — instead of saying the
        collection does not exist in the library being read.
        """
        with pytest.raises(ValueError, match="no Zotero collection named"):
            read_zotero(library, collection="Group Only Reading List")


def _group_notices(caught: pytest.WarningsRecorder) -> list[str]:
    """Just the group-library notices out of *caught*, as strings."""
    return [
        str(entry.message)
        for entry in caught
        if str(entry.message).startswith("this Zotero database also holds")
    ]


class TestGroupLibrariesAreAnnounced:
    """Scoping to the personal library must not be silent about it.

    The scoping itself is right and is pinned by :class:`TestLibraryScoping`.
    What was missing is the other half: a researcher whose reading group keeps
    its citations in a shared group library runs ``bibaudit`` against
    ``zotero.sqlite``, gets zero references back, and sees output identical to
    somebody whose library really is empty. Nothing separates "scoped away"
    from "nothing there" — and a clean report is what everyone hopes to see,
    so the run reads as a success and the bibliography is never checked at
    all.

    The counterweight is that a notice which cries wolf is worse than none, so
    half of these tests are about when it must stay quiet.
    """

    def test_the_notice_names_the_group_and_counts_what_was_skipped(
        self, library: pathlib.Path
    ) -> None:
        """A count and a name, because "some group libraries exist" is not
        enough for a user to know whether the run they just got is the run
        they wanted.
        """
        with pytest.warns(RuntimeWarning) as caught:
            read_zotero(library)
        notices = _group_notices(caught)
        assert len(notices) == 1
        assert "'MCC-Spain Reading Group' (2 items)" in notices[0]

    def test_the_notice_says_what_to_do_instead(
        self, library: pathlib.Path
    ) -> None:
        """A diagnosis with no remedy gets read once and then filtered out.

        The CSL-JSON export path is a real way to check a group library with
        this tool, so the notice names it.
        """
        with pytest.warns(RuntimeWarning) as caught:
            read_zotero(library)
        notice = _group_notices(caught)[0]
        assert "CSL JSON" in notice
        assert "My Library" in notice

    def test_a_group_holding_nothing_citable_is_not_named(
        self, library: pathlib.Path
    ) -> None:
        """Library 3 ("Slides and Handouts") holds one attachment.

        Most researchers belong to several groups that only ever accumulate
        slide decks and PDFs. Naming every one of them turns a five-word
        answer into a paragraph the reader skips, and the next time it is the
        group that mattered.
        """
        with pytest.warns(RuntimeWarning) as caught:
            read_zotero(library)
        assert "Slides and Handouts" not in _group_notices(caught)[0]

    def test_a_feed_subscription_is_not_named(self, library: pathlib.Path) -> None:
        """``libraries.type`` also takes ``'feed'``.

        Library 4 is a journal table-of-contents subscription holding one
        ``feedItem``. A notice written as ``type != 'user'`` would report it as
        an unread library of the user's citations, which it is not — and a
        user who then went looking for a "group library" by that name would
        find nothing.
        """
        with pytest.warns(RuntimeWarning) as caught:
            read_zotero(library)
        notice = _group_notices(caught)[0]
        assert "library 4" not in notice
        assert "1 group library" in notice

    def test_a_trashed_group_item_is_not_counted(self, library: pathlib.Path) -> None:
        """Item 102 is in library 2 *and* in the trash.

        The count has to mean "what you would have got", on the same terms the
        personal library is read on. Counting rows in ``items`` instead would
        promise three references from a group that would only ever yield two.
        """
        with pytest.warns(RuntimeWarning) as caught:
            read_zotero(library)
        assert "(2 items)" in _group_notices(caught)[0]

    def test_include_trashed_counts_the_group_trash_too(
        self, library: pathlib.Path
    ) -> None:
        """The other half of the same rule: the count follows the caller's own
        choice rather than a fixed one.
        """
        with pytest.warns(RuntimeWarning) as caught:
            read_zotero(library, include_trashed=True)
        assert "(3 items)" in _group_notices(caught)[0]

    def test_the_notice_still_fires_when_a_collection_filter_is_given(
        self, library: pathlib.Path
    ) -> None:
        """``--collection`` is where the confusion is *worst*.

        Collection names are per-library, so a user whose "Epidemiology"
        collection lives in the group library gets the personal library's
        same-named one instead — a plausible, non-empty, wrong answer. That is
        precisely when they need to be told another library exists.
        """
        with pytest.warns(RuntimeWarning) as caught:
            read_zotero(library, collection="Epidemiology")
        assert _group_notices(caught)

    def test_a_library_with_no_groups_at_all_says_nothing(
        self, tmp_path: pathlib.Path
    ) -> None:
        """The false-alarm guard, and the common case by a wide margin.

        Most Zotero users belong to no groups. A notice on every single run
        would be pure noise, and noise is what stops the notice being read on
        the one run it matters.
        """
        path = tmp_path / "zotero.sqlite"
        _build_library(path)
        conn = sqlite3.connect(path)
        try:
            conn.execute("DELETE FROM items WHERE libraryID != 1")
            conn.execute("DELETE FROM groups")
            conn.execute("DELETE FROM libraries WHERE libraryID != 1")
            conn.commit()
        finally:
            conn.close()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            refs = read_zotero(path)
        assert [str(entry.message) for entry in caught] == []
        assert refs, "the fixture's personal library should still have been read"

    def test_an_unnamed_group_falls_back_to_its_library_id(
        self, tmp_path: pathlib.Path
    ) -> None:
        """A ``libraries`` row can outlive or precede its ``groups`` row.

        Zotero writes the library before the group metadata arrives from the
        sync server, so a database captured mid-first-sync has the one without
        the other. Dropping the notice entirely in that window would restore
        exactly the silence this whole class exists to remove, so it degrades
        to the id instead.
        """
        path = tmp_path / "zotero.sqlite"
        _build_library(path)
        conn = sqlite3.connect(path)
        try:
            conn.execute("DROP TABLE groups")
            conn.commit()
        finally:
            conn.close()

        with pytest.warns(RuntimeWarning) as caught:
            read_zotero(path)
        assert "'library 2' (2 items)" in _group_notices(caught)[0]

    def test_the_group_items_are_still_not_returned(
        self, library: pathlib.Path
    ) -> None:
        """Announcing them is not reading them.

        Rule 2 in miniature: the tool reports what it found and changes
        nothing about what it does. If telling the user about the group
        library had also started merging it in, every assertion in
        :class:`TestLibraryScoping` would be back on the table.
        """
        with pytest.warns(RuntimeWarning):
            refs = read_zotero(library)
        assert "GROUPART2" not in {ref.key for ref in refs}
        assert len([ref for ref in refs if ref.key == "ARTICLE01"]) == 1


class TestCollectionFilter:
    def test_descendant_collections_are_included_to_any_depth(
        self, library: pathlib.Path
    ) -> None:
        """Filtering on a parent collection must reach its whole subtree.

        Users file by project and subdivide by chapter; a filter that matched
        only direct membership would return the handful of items left at the
        top level and quietly ignore the rest.

        The fixture nests three levels — Epidemiology > Shift work > Night
        work — because a one-step ``children[match]`` lookup satisfies a
        two-level test and still truncates a real library. ARTICLE01 is at the
        top, CHILDCOLL one down, GRANDKID1 two.
        """
        keys = {ref.key for ref in read_zotero(library, collection="Epidemiology")}
        assert keys == {"ARTICLE01", "CHILDCOLL", "GRANDKID1"}

    def test_filtering_on_the_middle_collection_takes_only_its_own_subtree(
        self, library: pathlib.Path
    ) -> None:
        """Naming "Shift work" must not drag its *parent's* items in too."""
        keys = {ref.key for ref in read_zotero(library, collection="Shift work")}
        assert keys == {"CHILDCOLL", "GRANDKID1"}

    def test_collection_name_match_is_case_insensitive(
        self, library: pathlib.Path
    ) -> None:
        keys = {ref.key for ref in read_zotero(library, collection="epidemiology")}
        assert keys == {"ARTICLE01", "CHILDCOLL", "GRANDKID1"}

    def test_items_outside_the_collection_tree_are_excluded(
        self, library: pathlib.Path
    ) -> None:
        keys = {ref.key for ref in read_zotero(library, collection="Epidemiology")}
        assert "OTHERCOLL" not in keys
        assert "BOOKSECT1" not in keys

    def test_an_unknown_collection_name_raises_rather_than_returning_nothing(
        self, library: pathlib.Path
    ) -> None:
        """An empty result reads as "nothing to check" and would be believed."""
        with pytest.raises(ValueError, match="no Zotero collection named 'Nonexistent'"):
            read_zotero(library, collection="Nonexistent")

    def test_a_trashed_item_inside_the_collection_is_still_excluded(
        self, library: pathlib.Path
    ) -> None:
        """TRASHED01 is filed in Methods, so the two filters have to compose.

        Zotero leaves a trashed item in its collections until the trash is
        emptied, so "in the collection" and "in the trash" are not exclusive.
        Applying the collection filter *instead of* the trash filter is the
        easy mistake, and it puts a reference the user deliberately deleted
        back into the report.
        """
        keys = {ref.key for ref in read_zotero(library, collection="Methods")}
        assert keys == {"OTHERCOLL"}

    def test_a_trashed_item_inside_the_collection_returns_with_include_trashed(
        self, library: pathlib.Path
    ) -> None:
        """The other half: the collection filter must not swallow the trash
        the caller explicitly asked to see.
        """
        keys = {
            ref.key
            for ref in read_zotero(library, collection="Methods", include_trashed=True)
        }
        assert keys == {"OTHERCOLL", "TRASHED01"}


class TestReadOnly:
    """Rule 2, at its sharpest: this is the user's live library."""

    def test_a_write_through_the_modules_own_connection_fails(
        self, library: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The connection ``_read_sqlite`` opens must reject a write.

        Asserted on the connection the module built, not a reconstruction of
        it. Zotero may be running while bibaudit reads; a write here can
        corrupt a library that took years to build.

        ``immutable=1`` and ``PRAGMA query_only`` each make a write fail on
        their own, so this test alone does *not* pin either of them — dropping
        one leaves it green. ``query_only`` is therefore checked explicitly
        below, and ``immutable=1`` by
        :meth:`test_reading_a_wal_mode_library_creates_no_sidecar_files`.
        """
        attempted: list[str] = []
        real = zotero._extract_references

        def spy(conn: sqlite3.Connection, **kwargs: Any) -> Any:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("INSERT INTO itemDataValues (valueID, value) VALUES (99, 'x')")
            # The belt-and-suspenders pragma the module documents. Without it,
            # read-only rests entirely on the URI, and the URI is built in a
            # different function from the one that opens the connection.
            assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
            attempted.append("write refused")
            return real(conn, **kwargs)

        monkeypatch.setattr(zotero, "_extract_references", spy)
        assert read_zotero(library)
        assert attempted == ["write refused"]

    def test_reading_a_wal_mode_library_creates_no_sidecar_files(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``immutable=1``, observed rather than asserted on a call.

        To read a WAL-mode database sqlite normally builds a ``-shm``
        shared-memory index and a ``-wal`` beside the file. Opened
        ``?mode=ro`` — the obvious-looking alternative, and one that passes
        every other test here — it creates both **and leaves them there**:
        bibaudit writing into the user's Zotero data directory, which is the
        one thing rule 2 forbids. Opened read-write it creates them too and
        merely tidies up on close. ``immutable=1`` tells sqlite the file
        cannot change, so it builds nothing and touches nothing.

        The directory is checked *during* the read as well as after, because
        the read-write case is clean by the time the connection closes.
        """
        path = tmp_path / "zotero.sqlite"
        _build_library(path, wal=True)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["zotero.sqlite"]

        during: list[list[str]] = []
        real = zotero._extract_references

        def spy(conn: sqlite3.Connection, **kwargs: Any) -> Any:
            during.append(sorted(p.name for p in tmp_path.iterdir()))
            return real(conn, **kwargs)

        monkeypatch.setattr(zotero, "_extract_references", spy)
        assert read_zotero(path)

        assert during == [["zotero.sqlite"]]
        assert sorted(p.name for p in tmp_path.iterdir()) == ["zotero.sqlite"]

    def test_the_connection_is_opened_with_the_immutable_uri(
        self, library: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLAUDE.md states the contract as a literal string, so pin the string.

        ``test_the_uri_is_immutable_and_percent_encoded`` only exercises
        ``_sqlite_uri`` in isolation; nothing else ties ``_read_sqlite`` to it,
        and a ``sqlite3.connect(str(path))`` that bypasses the helper entirely
        passes every behavioural test in this class on a legacy-journal
        database.
        """
        real = sqlite3.connect
        opened: list[tuple[str, bool]] = []

        def spy(target: Any, *args: Any, **kwargs: Any) -> Any:
            opened.append((str(target), bool(kwargs.get("uri"))))
            return real(target, *args, **kwargs)

        # The library is built before the spy is installed, so only the
        # adapter's own connect() is recorded.
        monkeypatch.setattr(sqlite3, "connect", spy)
        assert read_zotero(library)

        assert opened == [(library.resolve().as_uri() + "?immutable=1", True)]

    def test_reading_leaves_the_database_file_byte_identical(
        self, library: pathlib.Path
    ) -> None:
        """Not even an incidental write: no journal replay, no hot-page flush."""
        before = hashlib.sha256(library.read_bytes()).hexdigest()
        read_zotero(library)
        read_zotero(library, include_trashed=True, collection="Epidemiology")
        assert hashlib.sha256(library.read_bytes()).hexdigest() == before

    def test_the_uri_is_immutable_and_percent_encoded(self, tmp_path: pathlib.Path) -> None:
        """A hand-built ``f"file:{path}?immutable=1"`` breaks on any path with a
        space or a ``#`` in it — "My Documents", a synced-drive folder — because
        sqlite parses those as query string and fragment.
        """
        awkward = tmp_path / "My Zotero #1"
        awkward.mkdir()
        uri = zotero._sqlite_uri(awkward / "zotero.sqlite")
        assert uri.endswith("?immutable=1")
        assert "%20" in uri and "%23" in uri

    def test_a_library_in_an_awkwardly_named_directory_is_readable(
        self, tmp_path: pathlib.Path
    ) -> None:
        """End-to-end companion to the URI test: sqlite must actually open it."""
        awkward = tmp_path / "My Zotero #1"
        awkward.mkdir()
        _build_library(awkward / "zotero.sqlite")
        assert {ref.key for ref in read_zotero(awkward)} >= {"ARTICLE01"}


class TestMidWriteSnapshot:
    """A ``-wal`` or ``-journal`` sidecar means Zotero is mid-write."""

    @pytest.mark.parametrize("sidecar", ["-wal", "-journal"])
    def test_a_sidecar_makes_the_reader_open_a_copy(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, sidecar: str
    ) -> None:
        """Opening the live file while Zotero commits to it can read a torn
        page, and with ``immutable=1`` sqlite will not even notice: it is told
        to assume the file is not changing. Copying first is the only defence.
        """
        path = tmp_path / "zotero.sqlite"
        _build_library(path)
        path.with_name(path.name + sidecar).write_bytes(b"")

        opened = _opened_database_recorder(monkeypatch)
        with pytest.warns(RuntimeWarning, match="Zotero may be"):
            refs = read_zotero(path)

        assert opened and pathlib.Path(opened[0]) != path.resolve()
        # The copy is a real copy: the same library comes back out of it.
        assert {ref.key for ref in refs} >= {"ARTICLE01", "BOOKSECT1"}

    def test_the_snapshot_copy_is_deleted_afterwards(
        self, tmp_path: pathlib.Path
    ) -> None:
        """A 100 MB library copied per run and never removed fills a temp disk
        after a few audits, and the copy is a full readable duplicate of a
        private bibliography besides.
        """
        path = tmp_path / "zotero.sqlite"
        _build_library(path)
        path.with_name(path.name + "-wal").write_bytes(b"")

        with pytest.warns(RuntimeWarning) as record:
            read_zotero(path)

        snapshot = _snapshot_path_in(record)
        assert not snapshot.exists()
        assert not snapshot.parent.exists()

    def test_the_snapshot_is_removed_even_when_the_copy_cannot_be_opened(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``sqlite3.connect`` raises eagerly on a database it cannot open.

        The copy has already been made by then, so a cleanup that only guards
        the queries abandons a complete, readable duplicate of the user's
        bibliography in the system temp directory — permanently, since nothing
        holds a reference to it any more.
        """
        path = tmp_path / "zotero.sqlite"
        _build_library(path)
        path.with_name(path.name + "-wal").write_bytes(b"")

        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise sqlite3.OperationalError("unable to open database file")

        # Installed after the library is built, so only the adapter's own
        # connect() is affected.
        monkeypatch.setattr(sqlite3, "connect", refuse)
        with pytest.warns(RuntimeWarning) as record, pytest.raises(sqlite3.OperationalError):
            read_zotero(path)

        snapshot = _snapshot_path_in(record)
        assert not snapshot.exists()
        assert not snapshot.parent.exists()

    def test_a_failed_snapshot_copy_leaves_nothing_behind(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A copy that dies part-way through — a full temp disk, the usual
        cause on a laptop with a 40 GB library — still writes some of the
        user's bibliography to the temp directory before it fails, and the
        warning naming the copy is never reached, so nothing else could ever
        find it to clean it up.

        The temp directory is learned from ``tempfile.mkdtemp`` rather than by
        globbing for the module's prefix, so a concurrent audit's snapshot
        cannot make this pass by accident.
        """
        path = tmp_path / "zotero.sqlite"
        _build_library(path)
        path.with_name(path.name + "-wal").write_bytes(b"")

        created: list[pathlib.Path] = []
        real_mkdtemp = tempfile.mkdtemp

        def record(*args: Any, **kwargs: Any) -> str:
            made = str(real_mkdtemp(*args, **kwargs))
            created.append(pathlib.Path(made))
            return made

        def full_disk(*args: Any, **kwargs: Any) -> Any:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(tempfile, "mkdtemp", record)
        monkeypatch.setattr(shutil, "copy2", full_disk)

        with pytest.raises(OSError, match="No space left"):
            read_zotero(path)

        assert created, "the reader never took a snapshot at all"
        assert not created[0].exists()

    def test_the_original_is_untouched_by_the_snapshot_path(
        self, tmp_path: pathlib.Path
    ) -> None:
        path = tmp_path / "zotero.sqlite"
        _build_library(path)
        path.with_name(path.name + "-wal").write_bytes(b"")
        before = hashlib.sha256(path.read_bytes()).hexdigest()

        with pytest.warns(RuntimeWarning):
            read_zotero(path)

        assert hashlib.sha256(path.read_bytes()).hexdigest() == before

    def test_without_a_sidecar_the_live_file_is_read_directly(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No snapshot warning and no copy in the ordinary case: warning every
        time would train users to ignore the one run where it matters.

        Scoped to the mid-write warning rather than to RuntimeWarning as a
        category. The fixture library also holds group libraries, which raise
        their own, unrelated notice; treating *any* RuntimeWarning as the
        failure would make this test fail for a reason it says nothing about.
        """
        path = tmp_path / "zotero.sqlite"
        _build_library(path)
        opened = _opened_database_recorder(monkeypatch)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            read_zotero(path)

        assert not [w for w in caught if "point-in-time copy" in str(w.message)]
        assert opened == [str(path.resolve())]


class TestPathResolution:
    def test_a_data_directory_resolves_to_its_zotero_sqlite(
        self, tmp_path: pathlib.Path
    ) -> None:
        _build_library(tmp_path / "zotero.sqlite")
        assert {ref.key for ref in read_zotero(tmp_path)} == {
            ref.key for ref in read_zotero(tmp_path / "zotero.sqlite")
        }

    def test_a_directory_without_a_library_is_a_clear_error(
        self, tmp_path: pathlib.Path
    ) -> None:
        with pytest.raises(FileNotFoundError, match=r"no zotero\.sqlite found"):
            read_zotero(tmp_path)

    def test_a_missing_file_is_a_clear_error(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(FileNotFoundError, match="does not exist"):
            read_zotero(tmp_path / "nope.sqlite")


class TestDefaultPaths:
    """Never depends on the machine's real ``~/Zotero``; home is redirected.

    ``sys`` is replaced inside the module rather than ``sys.platform`` being
    patched in place: the real attribute is process-global, and a test that
    lies about the platform to everything else running in the interpreter is
    a bad neighbour even for the microsecond it holds.
    """

    @staticmethod
    def _pretend(monkeypatch: pytest.MonkeyPatch, platform: str, home: pathlib.Path) -> None:
        monkeypatch.setattr(zotero, "sys", types.SimpleNamespace(platform=platform))
        monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))

    def test_only_directories_holding_a_library_are_returned(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The docstring promises ``paths[0]`` is safe when the list is
        non-empty, so returning a directory that merely exists would hand the
        caller a path that then fails to open.
        """
        self._pretend(monkeypatch, "darwin", tmp_path)

        (tmp_path / "Zotero").mkdir()
        assert default_zotero_paths() == []

        _build_library(tmp_path / "Zotero" / "zotero.sqlite")
        assert default_zotero_paths() == [tmp_path / "Zotero"]

    def test_windows_looks_under_documents_first(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Current Windows installs default to Documents/Zotero; older ones did
        not, so both are searched and the current default comes first.
        """
        self._pretend(monkeypatch, "win32", tmp_path)

        (tmp_path / "Documents" / "Zotero").mkdir(parents=True)
        _build_library(tmp_path / "Documents" / "Zotero" / "zotero.sqlite")
        (tmp_path / "Zotero").mkdir()
        _build_library(tmp_path / "Zotero" / "zotero.sqlite")

        assert default_zotero_paths() == [
            tmp_path / "Documents" / "Zotero",
            tmp_path / "Zotero",
        ]


@pytest.fixture(scope="module")
def csl() -> dict[str, Reference]:
    return {ref.key: ref for ref in read_csl_json(_DATA / "zotero_csl_export.json")}


class TestCslJson:
    """``File > Export Library > CSL JSON``."""

    def test_scalar_fields_map_onto_the_reference(
        self, csl: dict[str, Reference]
    ) -> None:
        """CSL spells several of these differently from Zotero's own JSON:
        ``page`` not ``pages``, ``URL`` not ``url``, ``container-title`` not
        ``publicationTitle``. Reusing the Zotero names silently empties them.
        """
        ref = csl["papantoniou2017"]
        assert ref.kind == "article"
        assert ref.title == (
            "Shift work and colorectal cancer risk in the MCC-Spain case-control study"
        )
        assert ref.container == "American Journal of Epidemiology"
        assert ref.volume == "186"
        assert ref.issue == "5"
        assert ref.pages == "533-540"
        assert ref.doi == "10.1093/aje/kwx137"
        assert ref.url == "https://doi.org/10.1093/aje/kwx137"
        assert ref.locator == "zotero:papantoniou2017"

    def test_authors_keep_their_order_and_split(self, csl: dict[str, Reference]) -> None:
        assert [(a.family, a.given) for a in csl["papantoniou2017"].authors] == [
            ("Papantoniou", "Kyriaki"),
            ("Castano-Vinyals", "Gemma"),
        ]

    def test_a_literal_author_is_one_collective_name(
        self, csl: dict[str, Reference]
    ) -> None:
        """CSL's ``literal`` is the collective-author escape hatch; splitting
        it on " and " invents two authors out of one working group.
        """
        authors = csl["ehbccg2002"].authors
        assert len(authors) == 1
        assert authors[0].collective
        assert authors[0].literal == (
            "The Endogenous Hormones and Breast Cancer Collaborative Group"
        )

    def test_a_string_year_inside_date_parts_is_accepted(
        self, csl: dict[str, Reference]
    ) -> None:
        """Zotero emits ``[["2017", 8, 15]]`` — the year quoted — where the CSL
        spec allows an integer. Only accepting ``int`` loses the year silently.
        """
        assert csl["papantoniou2017"].year == 2017

    def test_an_integer_year_is_accepted(self, csl: dict[str, Reference]) -> None:
        assert csl["ehbccg2002"].year == 2002

    def test_a_raw_date_is_accepted(self, csl: dict[str, Reference]) -> None:
        """``raw`` is CSL's fallback for a date no processor could decompose."""
        assert csl["hidalgo2015"].year == 2015

    def test_editor_substitutes_when_there_is_no_author_array(
        self, csl: dict[str, Reference]
    ) -> None:
        ref = csl["hidalgo2015"]
        assert [a.family for a in ref.authors] == ["Hidalgo"]
        assert ref.raw["creator_role"] == "editor"
        assert ref.kind == "book"
        assert ref.isbn == "9780190238667"
        assert ref.publisher == "Oxford University Press"

    def test_zotero_native_json_is_refused_rather_than_read_as_csl(self) -> None:
        """Every CSL field name would miss, producing References with nothing
        in them — which the audit would then report as a bibliography full of
        incomplete entries rather than as the wrong file being passed.
        """
        with pytest.raises(ValueError, match="itemType"):
            read_csl_json(_DATA / "zotero_native_items.json")


class TestJsonDispatch:
    def test_native_item_json_is_detected_and_read(self) -> None:
        refs = read_zotero(_DATA / "zotero_native_items.json")
        assert [ref.key for ref in refs] == ["ABCD1234"]
        assert refs[0].container == "American Journal of Epidemiology"
        assert refs[0].year == 2017

    def test_native_json_single_field_creator_is_collective(self) -> None:
        """fieldMode 1 exports as ``{"name": ...}`` with no first/last split."""
        authors = read_zotero(_DATA / "zotero_native_items.json")[0].authors
        assert [a.collective for a in authors] == [False, True]
        assert authors[1].literal == (
            "The Endogenous Hormones and Breast Cancer Collaborative Group"
        )

    def test_attachments_and_notes_are_skipped_in_native_json_too(self) -> None:
        assert len(read_zotero(_DATA / "zotero_native_items.json")) == 1

    def test_csl_json_is_detected_and_read(self) -> None:
        refs = read_zotero(_DATA / "zotero_csl_export.json")
        assert {ref.key for ref in refs} == {"papantoniou2017", "ehbccg2002", "hidalgo2015"}

    def test_collection_filtering_on_a_json_export_is_refused(self) -> None:
        """A CSL export carries no collection-name table, so a name cannot be
        resolved from the file. Returning everything instead would be worse:
        the user asked for a subset and would be told nothing went wrong.
        """
        with pytest.raises(ValueError, match="collection filtering needs a live source"):
            read_zotero(_DATA / "zotero_csl_export.json", collection="Epidemiology")


class TestLocalApi:
    """Zotero's local read-only HTTP API, stubbed at ``urlopen``."""

    @staticmethod
    def _item(key: str, item_type: str = "journalArticle", **fields: Any) -> dict[str, Any]:
        data = {"key": key, "itemType": item_type, "title": f"Item {key}"}
        data.update(fields)
        return {"key": key, "data": data}

    def _install(
        self, monkeypatch: pytest.MonkeyPatch, pages: dict[str, list[list[dict[str, Any]]]]
    ) -> list[urllib.request.Request]:
        """Serve *pages* (path -> successive response bodies) from urlopen."""
        seen: list[urllib.request.Request] = []
        counters: dict[str, int] = {}

        class _Response:
            def __init__(self, payload: Any) -> None:
                self._body = json.dumps(payload).encode("utf-8")

            def read(self) -> bytes:
                return self._body

            def __enter__(self) -> _Response:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        def fake_urlopen(request: urllib.request.Request, timeout: float | None = None) -> Any:
            seen.append(request)
            path = request.full_url.split("/api/users/0", 1)[1].split("?", 1)[0]
            index = counters.get(path, 0)
            counters[path] = index + 1
            bodies = pages.get(path, [[]])
            return _Response(bodies[min(index, len(bodies) - 1)])

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        return seen

    def test_pagination_is_followed_to_the_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The API caps a response at the requested limit.

        Stopping after the first page truncates a library at 100 items and
        reports the rest as absent — a silent under-count, which is the worst
        failure mode a verifier can have.
        """
        first = [self._item(f"K{i:04d}") for i in range(100)]
        second = [self._item("LAST0001"), self._item("PDF00001", "attachment")]
        self._install(monkeypatch, {"/items": [first, second]})

        refs = read_zotero("local")
        assert len(refs) == 101
        assert refs[-1].key == "LAST0001"

    def test_the_allowed_request_header_is_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Zotero 7 rejects local API calls without it, so a library that is
        right there would read as unreachable.
        """
        seen = self._install(monkeypatch, {"/items": [[]]})
        read_zotero("local")
        assert seen
        # The value matters, not just the key: Zotero checks for a truthy
        # value, so an empty header is refused exactly like a missing one.
        for request in seen:
            headers = {k.lower(): v for k, v in request.headers.items()}
            assert headers.get("zotero-allowed-request") == "true"

    def test_the_trash_is_only_requested_when_asked_for(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._install(
            monkeypatch, {"/items": [[self._item("LIVE0001")]], "/items/trash": [[self._item("BIN00001")]]}
        )

        assert {ref.key for ref in read_zotero("local")} == {"LIVE0001"}
        assert not any("/items/trash" in r.full_url for r in seen)

        keys = {ref.key for ref in read_zotero("local", include_trashed=True)}
        assert keys == {"LIVE0001", "BIN00001"}

    def test_an_unreachable_client_gives_an_actionable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Zotero not running is the overwhelmingly likely cause; a bare
        URLError traceback tells the user nothing they can act on.
        """

        def refuse(request: urllib.request.Request, timeout: float | None = None) -> Any:
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

        monkeypatch.setattr(urllib.request, "urlopen", refuse)
        with pytest.raises(RuntimeError, match="Start Zotero"):
            read_zotero("local")

    def test_an_http_error_reports_the_status_it_got(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 500 from a running client is a different problem from a client
        that is not running, and the two advice strings are different: there
        is nothing for the user to switch on here. Reporting the status is
        what makes the difference visible.
        """

        def fail(request: urllib.request.Request, timeout: float | None = None) -> Any:
            raise urllib.error.HTTPError(
                request.full_url, 500, "Internal Server Error", {}, None  # type: ignore[arg-type]
            )

        monkeypatch.setattr(urllib.request, "urlopen", fail)
        with pytest.raises(RuntimeError, match=r"HTTP 500"):
            read_zotero("local")
