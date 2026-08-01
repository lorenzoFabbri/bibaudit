"""Read a Zotero library into :class:`~bibaudit.model.Reference` objects.

Three ways in, all producing the same shape:

``read_zotero(path_to_zotero.sqlite_or_its_directory)``
    Opens the user's live database directly. Read-only, always — see the
    module-level notes on ``immutable=1`` below; this is the one place in
    ``bibaudit`` that touches an application database it does not own.

``read_zotero(path_to_a_.json_export)``
    Either shape a user is likely to hand this tool: a CSL-JSON bibliography
    ("File > Export Library > CSL JSON" in Zotero, or Better BibTeX's export)
    or a raw dump of Zotero's own item JSON (what the API and the local
    connector return). The two are told apart by the ``itemType`` key, which
    only Zotero's own shape has — CSL always calls it ``type``.

``read_zotero("local")``
    Talks to the Zotero desktop client's built-in read-only HTTP API on
    ``127.0.0.1:23119``, the same one Zotero's own connector uses. Nothing is
    written through it — see :func:`bibaudit.adapters.zotero` docs at
    https://www.zotero.org/support/dev/client_coding/javascript_api for the
    "no writes" guarantee this relies on.

None of the three assumes anything about a particular library: field IDs,
item-type IDs and collection hierarchies are all resolved by name at read
time, because Zotero's schema numbers them differently across versions and a
library synced since 2015 has been through several.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import warnings
from collections import defaultdict
from typing import Any

from ..model import Name, Reference
from ..normalize import clean, normalize_doi, normalize_kind, parse_year

__all__ = ["default_zotero_paths", "read_csl_json", "read_zotero"]

#: Zotero item types that are not citable works in their own right: a PDF
#: attachment, a note, or a PDF annotation. Reading these in as References
#: would mean a library with one journal article and three attached PDFs
#: reports as four incomplete citations instead of one real one.
_NON_BIBLIOGRAPHIC_ITEM_TYPES = frozenset({"attachment", "note", "annotation"})

#: Zotero's local HTTP API. Documented as a read replica of the web API
#: (https://www.zotero.org/support/dev/client_coding/javascript_api) — it has
#: no item-creation or item-modification routes, which is what makes calling
#: it here compatible with rule 2 ("never write to the user's Zotero data").
_LOCAL_API_BASE = "http://127.0.0.1:23119/api/users/0"
_LOCAL_API_PAGE_SIZE = 100
_LOCAL_API_TIMEOUT_SECONDS = 10.0


def read_zotero(
    source: pathlib.Path | str,
    *,
    include_trashed: bool = False,
    collection: str | None = None,
) -> list[Reference]:
    """Read a Zotero library, detecting the source kind from *source*.

    *source* is one of: a path to ``zotero.sqlite`` or a data directory
    containing one; a path to a ``.json`` export (CSL-JSON or Zotero's own
    item JSON); or the literal string ``"local"`` for Zotero's local API.

    *collection* filters to items directly in, or in a descendant of, the
    named collection (case-insensitive match on the collection's display
    name). It requires a live source — a JSON export carries collections only
    as opaque keys with no accompanying name table, so a name cannot be
    resolved from the file alone.

    *include_trashed* only has an effect on a live source. A ``.json`` export
    is read exactly as saved: a CSL-JSON bibliography has no trash concept at
    all, and a saved dump of Zotero's own item JSON contains whatever items
    were in it when it was made, with no per-item flag this module can use to
    tell a trashed item from a live one after the fact.

    Both live sources read **only the personal library** ("My Library"): the
    local API exposes nothing else, and the sqlite path scopes to it
    deliberately (see :func:`_personal_library_id`). A sqlite database that
    also holds synced *group* libraries produces a ``RuntimeWarning`` naming
    them, because an empty result and a result that was scoped away look
    identical otherwise — see :func:`_warn_about_unread_group_libraries`.
    """
    if isinstance(source, str) and source.strip().lower() == "local":
        return _read_local(include_trashed=include_trashed, collection=collection)

    path = pathlib.Path(source)
    if path.suffix.lower() == ".json":
        if collection is not None:
            raise ValueError(
                "collection filtering needs a live source (a zotero.sqlite "
                "path or \"local\"); a JSON export has no collection-name "
                "table to resolve against"
            )
        return _read_json_export(path)

    return _read_sqlite(_resolve_sqlite_path(path), include_trashed=include_trashed, collection=collection)


def default_zotero_paths() -> list[pathlib.Path]:
    """Zotero data directories in their platform-conventional locations that exist.

    Zotero's own default moved during its history — early Windows installs
    used a profile-relative folder, current ones default to
    ``Documents/Zotero`` — and a user can always relocate the data directory
    from Preferences besides, so this is a best-effort list, not a guarantee.
    Only directories that actually contain a ``zotero.sqlite`` are returned,
    most-likely-first, so a caller can safely use ``paths[0]`` when non-empty.
    """
    home = pathlib.Path.home()
    if sys.platform == "darwin":
        candidates = [home / "Zotero"]
    elif sys.platform.startswith("win"):
        candidates = [home / "Documents" / "Zotero", home / "Zotero"]
    else:
        candidates = [home / "Zotero"]
    return [p for p in candidates if (p / "zotero.sqlite").exists()]


def read_csl_json(path: pathlib.Path) -> list[Reference]:
    """Read a CSL-JSON bibliography (Zotero's "CSL JSON" export format).

    Raises :class:`ValueError` if *path* instead holds Zotero's own item JSON
    (recognisable by the ``itemType`` key CSL never uses) — silently reading
    it as CSL would produce References with every field empty rather than a
    clear failure, since none of CSL's field names would match.
    """
    path = pathlib.Path(path)
    items = _unwrap_json_items(_load_json(path))
    if items and _is_zotero_native_shape(items[0]):
        raise ValueError(
            f"{path} looks like Zotero's own item JSON (has \"itemType\"), "
            "not CSL-JSON; pass it to read_zotero() instead, which "
            "auto-detects the shape"
        )
    return [_csl_item_to_reference(item) for item in items]


# --------------------------------------------------------------------------
# JSON file dispatch: CSL-JSON vs. Zotero's own item JSON.
# --------------------------------------------------------------------------


def _load_json(path: pathlib.Path) -> Any:
    """Parse *path* as JSON, or fail with the path attached to the error."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FileNotFoundError(f"cannot read {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc


def _unwrap_json_items(payload: Any) -> list[dict[str, Any]]:
    """Normalise the shapes a ``.json`` export can arrive in.

    A bare top-level array is what both Zotero's "CSL JSON" export and a
    dump of a local/web API response look like. Some export tools instead
    wrap it as ``{"items": [...]}``; that shape is accepted too rather than
    raising, since nothing about it is ambiguous.
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _is_zotero_native_shape(item: dict[str, Any]) -> bool:
    """True if *item* is Zotero's own item JSON rather than CSL-JSON.

    Zotero's API wraps each item as ``{"key": ..., "data": {...}}``; a saved
    export may or may not keep that wrapper. Either way, the "data" fields
    use ``itemType`` — a key CSL-JSON never has, since CSL calls the same
    concept ``type``. That makes it a safe discriminator: false positives
    would require a CSL-JSON producer to invent a same-named field, which
    would itself already violate the CSL spec.
    """
    data = item.get("data", item)
    return isinstance(data, dict) and "itemType" in data


def _read_json_export(path: pathlib.Path) -> list[Reference]:
    payload = _load_json(path)
    items = _unwrap_json_items(payload)
    if items and _is_zotero_native_shape(items[0]):
        refs = []
        for item in items:
            data = item.get("data", item)
            if data.get("itemType") in _NON_BIBLIOGRAPHIC_ITEM_TYPES:
                continue
            refs.append(_zotero_json_item_to_reference(data))
        return refs
    return read_csl_json(path)


def _select_creator_role(
    creators: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """Pick the creators to treat as authors, per the SQLite adapter's rule.

    Zotero bylines commonly have no ``author``-typed creator at all — an
    edited volume cited by its editors, say — and reporting that as "zero
    authors" is a worse answer than substituting the editors and saying so.
    The empty string return means "the normal case, no substitution", so a
    caller only pays attention to the role when it is non-empty.
    """
    authors = [c for c in creators if c.get("creatorType") == "author"]
    if authors:
        return authors, ""
    editors = [c for c in creators if c.get("creatorType") == "editor"]
    if editors:
        return editors, "editor"
    return [], ""


def _json_creator_to_name(creator: dict[str, Any]) -> Name:
    """Zotero's own creator JSON: ``{firstName, lastName}`` or single-field ``name``.

    A creator with ``fieldMode`` 1 in the database is exported as
    ``{"creatorType": ..., "name": "..."}`` instead of first/last name
    fields — that is how a corporate byline ("World Health Organization")
    survives the round trip without being torn into a given and family name.
    """
    name = creator.get("name")
    if name:
        return Name(literal=clean(name), collective=True)
    return Name(family=clean(creator.get("lastName", "")), given=clean(creator.get("firstName", "")))


def _zotero_json_item_to_reference(data: dict[str, Any]) -> Reference:
    """Build a Reference from one item's Zotero-shaped ``data`` object.

    Shared by the local-API reader and the native-JSON-export branch of
    :func:`_read_json_export`, since both hand over exactly this shape.
    """
    item_key = str(data.get("key") or "")
    type_name = str(data.get("itemType") or "")
    creators = list(data.get("creators") or [])
    author_creators, role = _select_creator_role(creators)
    container = data.get("publicationTitle") or data.get("bookTitle")

    raw: dict[str, Any] = {"itemType": type_name, "key": item_key, "fields": data, "creators": creators}
    if role:
        raw["creator_role"] = role

    return Reference(
        key=item_key or "unknown",
        locator=f"zotero:{item_key}" if item_key else "zotero:unknown",
        kind=normalize_kind(type_name),
        doi=normalize_doi(data["DOI"]) if data.get("DOI") else None,
        isbn=clean(data["ISBN"]) if data.get("ISBN") else None,
        url=clean(data["url"]) if data.get("url") else None,
        title=clean(data["title"]) if data.get("title") else None,
        authors=[_json_creator_to_name(c) for c in author_creators],
        year=parse_year(data["date"]) if data.get("date") else None,
        container=clean(container) if container else None,
        volume=clean(data["volume"]) if data.get("volume") else None,
        issue=clean(data["issue"]) if data.get("issue") else None,
        pages=clean(data["pages"]) if data.get("pages") else None,
        publisher=clean(data["publisher"]) if data.get("publisher") else None,
        raw=raw,
    )


def _csl_creator_to_name(creator: dict[str, Any]) -> Name:
    if creator.get("literal"):
        return Name(literal=clean(creator["literal"]), collective=True)
    return Name(family=clean(creator.get("family", "")), given=clean(creator.get("given", "")))


def _select_csl_creators(item: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Pick the creators to treat as authors from a CSL-JSON item.

    CSL-JSON gives each creator role its own top-level array (``author``,
    ``editor``, ...) rather than Zotero's per-creator ``creatorType``, but the
    same gap applies: an edited volume with no ``author`` key at all is
    common, and reporting that as "zero authors" is a worse answer than
    substituting the editors and saying so via ``creator_role`` -- exactly
    the rule :func:`_select_creator_role` applies for Zotero's own JSON shape
    and the sqlite reader's ``_build_reference_from_row``.
    """
    authors = item.get("author")
    if authors:
        return authors, ""
    editors = item.get("editor")
    if editors:
        return editors, "editor"
    return [], ""


def _csl_year(issued: Any) -> int | None:
    """First year out of a CSL ``issued`` date field, in any of its shapes."""
    if not isinstance(issued, dict):
        return None
    parts = issued.get("date-parts")
    if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
        year = parts[0][0]
        if isinstance(year, int):
            return year
        return parse_year(year)
    # "raw"/"literal" are CSL's fallback forms for a date a processor could
    # not decompose into date-parts; parse_year handles free-text dates.
    return parse_year(issued.get("raw") or issued.get("literal"))


def _csl_item_to_reference(item: dict[str, Any]) -> Reference:
    item_id = str(item.get("id") or "") or "unknown"
    type_name = str(item.get("type") or "")
    author_creators, role = _select_csl_creators(item)
    raw: dict[str, Any] = {"itemType": type_name, "key": item_id, "fields": item}
    if role:
        raw["creator_role"] = role

    return Reference(
        key=item_id,
        locator=f"zotero:{item_id}",
        kind=normalize_kind(type_name),
        doi=normalize_doi(item["DOI"]) if item.get("DOI") else None,
        isbn=clean(item["ISBN"]) if item.get("ISBN") else None,
        url=clean(item["URL"]) if item.get("URL") else None,
        title=clean(item["title"]) if item.get("title") else None,
        authors=[_csl_creator_to_name(c) for c in author_creators],
        year=_csl_year(item.get("issued")),
        container=clean(item["container-title"]) if item.get("container-title") else None,
        volume=clean(item["volume"]) if item.get("volume") else None,
        issue=clean(item["issue"]) if item.get("issue") else None,
        pages=clean(item["page"]) if item.get("page") else None,
        publisher=clean(item["publisher"]) if item.get("publisher") else None,
        raw=raw,
    )


# --------------------------------------------------------------------------
# zotero.sqlite
# --------------------------------------------------------------------------


def _resolve_sqlite_path(path: pathlib.Path) -> pathlib.Path:
    if path.is_dir():
        candidate = path / "zotero.sqlite"
        if not candidate.exists():
            raise FileNotFoundError(f"no zotero.sqlite found in {path}")
        return candidate
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist")
    return path


def _snapshot_if_mid_write(path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path | None]:
    """Read *path* directly, unless Zotero looks like it is writing to it.

    A ``-journal`` sidecar means a legacy rollback-journal transaction is in
    flight and the main file may hold a torn page; a ``-wal`` sidecar means
    Zotero is running in write-ahead-log mode with recent commits living
    outside the main file. Either way the live file is not a safe thing to
    open, so a snapshot copy is read instead. The copy is not a synchronised
    point-in-time backup — a WAL checkpoint is not forced — only a guarantee
    against reading a database that is being written to as it is read.

    Returns the path to actually open, and the temp directory to clean up
    afterwards (``None`` if no copy was made).
    """
    journal = path.with_name(path.name + "-journal")
    wal = path.with_name(path.name + "-wal")
    if not journal.exists() and not wal.exists():
        return path, None

    tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="bibaudit-zotero-"))
    snapshot = tmp_dir / path.name
    try:
        shutil.copy2(path, snapshot)
    except OSError:
        # A failed copy (a full temp disk is the usual cause) would otherwise
        # leave a *partial* readable duplicate of a private bibliography in
        # the system temp directory, with nothing left holding a reference to
        # it that could clean it up later.
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    warnings.warn(
        f"{path} has a pending -journal/-wal file, meaning Zotero may be "
        f"writing to it right now; reading a point-in-time copy at "
        f"{snapshot} instead of the live database.",
        RuntimeWarning,
        stacklevel=2,
    )
    return snapshot, tmp_dir


def _sqlite_uri(path: pathlib.Path) -> str:
    """A ``file:`` URI for *path* with ``immutable=1``, safely encoded.

    ``f"file:{path}?immutable=1"`` breaks the moment the path contains a
    character URIs treat specially — a space (any "My Documents"-style
    folder), a ``#`` (common in synced-drive paths), or a literal ``?`` —
    because those get parsed as part of the query string or a fragment
    instead of the path. ``Path.as_uri()`` percent-encodes correctly; only
    the ``?immutable=1`` suffix is added by hand, which is safe because it
    contains none of those characters itself.
    """
    return path.resolve().as_uri() + "?immutable=1"


def _personal_library_id(cursor: sqlite3.Cursor) -> int | None:
    """The ``libraryID`` of the user's own ("My Library") library, if resolvable.

    A synced account has one personal library plus one row per group library
    it belongs to, all sharing the same ``items``/``collections`` tables and
    the same *per-library* key namespace: two unrelated items in two
    different libraries can legitimately have the identical Zotero ``key``,
    and two unrelated collections can legitimately share a name. Reading
    every library unfiltered would silently merge a group library into the
    personal one under those same keys/names -- exactly what the "local" API
    path never does, since Zotero's local connector only ever exposes
    ``/api/users/0/...`` (the personal library). Scoping the sqlite path to
    ``type = 'user'`` keeps the two live-reading paths in agreement.

    Returns ``None`` (meaning: do not filter) if the ``libraries`` table is
    missing or has no personal library, rather than raising -- a database
    from a schema this old predates every other assumption this module
    already makes, and failing open here is strictly safer than the
    alternative of returning zero references from a library that exists.
    """
    try:
        row = cursor.execute("SELECT libraryID FROM libraries WHERE type = 'user' LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    return row["libraryID"] if row is not None else None


def _group_libraries(cursor: sqlite3.Cursor) -> dict[int, str]:
    """``libraryID -> display name`` for every synced *group* library present.

    The name comes from Zotero's ``groups`` table, which is the only place a
    group's human-readable title is stored; ``libraries`` knows the id and the
    type and nothing else. A LEFT JOIN rather than an inner one so a
    half-synced group — a ``libraries`` row whose ``groups`` row has not
    arrived yet — is still counted, with an empty name the caller renders as
    ``library <id>``.

    Restricted to ``type = 'group'``. ``libraries.type`` also takes ``'feed'``
    (RSS feed items, which are not citable works) and ``'publications'`` (My
    Publications), and neither is a place a user's bibliography lives, so
    naming them in the warning would be the noise that stops the warning
    being read.

    Returns ``{}`` rather than raising when either table is missing: this
    whole path is a courtesy message, and a schema old enough to lack
    ``groups`` predates group libraries anyway.
    """
    try:
        rows = cursor.execute(
            "SELECT libraries.libraryID AS libraryID, groups.name AS name "
            "FROM libraries LEFT JOIN groups ON groups.libraryID = libraries.libraryID "
            "WHERE libraries.type = 'group'"
        ).fetchall()
    except sqlite3.OperationalError:
        try:
            rows = cursor.execute("SELECT libraryID FROM libraries WHERE type = 'group'").fetchall()
        except sqlite3.OperationalError:
            return {}
        return {row["libraryID"]: "" for row in rows}
    return {row["libraryID"]: clean(row["name"] or "") for row in rows}


def _warn_about_unread_group_libraries(
    cursor: sqlite3.Cursor, *, library_id: int, trashed_item_ids: set[int]
) -> None:
    """Say out loud that group libraries exist and were skipped.

    The personal-library scoping in :func:`_personal_library_id` is correct
    and must stay, but on its own it fails silently in the one case where it
    changes the answer: a researcher whose reading group keeps its citations
    in a shared group library runs ``bibaudit check --zotero ...``, gets back
    zero references, and sees the exact same output as somebody whose library
    really is empty. Nothing distinguishes "scoped away" from "nothing there",
    and a clean report is what everyone wants to see — so the run reads as a
    success and the bibliography goes unchecked.

    Only libraries that actually hold something readable are named. A group a
    user belongs to but has never put an item in, and one holding only
    attachments and notes, are both counted as zero and stay silent; warning
    about them would be a false alarm in a message whose only job is to be
    believed. Trashed items are discounted on exactly the terms the personal
    library's own items are, so the count is "what you would have got", not
    "rows in the table".
    """
    groups = _group_libraries(cursor)
    if not groups:
        return

    skip_types = ",".join("?" for _ in _NON_BIBLIOGRAPHIC_ITEM_TYPES)
    counts: dict[int, int] = defaultdict(int)
    for row in cursor.execute(
        "SELECT items.itemID AS itemID, items.libraryID AS libraryID "
        "FROM items JOIN itemTypes ON itemTypes.itemTypeID = items.itemTypeID "
        f"WHERE itemTypes.typeName NOT IN ({skip_types}) AND items.libraryID <> ?",
        (*_NON_BIBLIOGRAPHIC_ITEM_TYPES, library_id),
    ):
        if row["libraryID"] in groups and row["itemID"] not in trashed_item_ids:
            counts[row["libraryID"]] += 1
    if not counts:
        return

    parts: list[str] = []
    for lib, count in sorted(counts.items()):
        label = groups[lib] or f"library {lib}"
        parts.append(f"{label!r} ({count} item{'' if count == 1 else 's'})")
    named = ", ".join(parts)
    plural = "library" if len(counts) == 1 else "libraries"
    warnings.warn(
        f"this Zotero database also holds {len(counts)} group {plural} that "
        f"bibaudit did not read: {named}. Only your personal library ('My "
        "Library') is read, because Zotero item keys and collection names are "
        "unique only within a library and merging two libraries would "
        "silently file one library's work under another's key. If the "
        "citations you meant to check live in a group library, export it from "
        "Zotero (File > Export Library, format 'CSL JSON') and pass the "
        "exported .json file to bibaudit instead.",
        RuntimeWarning,
        stacklevel=2,
    )


def _resolve_collection_ids(cursor: sqlite3.Cursor, name: str, library_id: int | None) -> set[int]:
    """collectionIDs for every collection named *name* (case-insensitive), plus their descendants.

    Restricted to *library_id* when known, so a group library's same-named
    collection (see :func:`_personal_library_id`) is not mistaken for the
    personal one.
    """
    try:
        rows = cursor.execute(
            "SELECT collectionID, collectionName, parentCollectionID, libraryID FROM collections"
        ).fetchall()
    except sqlite3.OperationalError:
        # A schema old enough to lack libraries.type (see _personal_library_id)
        # lacks collections.libraryID too, and library_id will already be
        # None in that case; the retry just satisfies that same assumption
        # for the column list, not a second, independent fallback.
        rows = cursor.execute("SELECT collectionID, collectionName, parentCollectionID FROM collections").fetchall()
        library_id = None
    if library_id is not None:
        rows = [row for row in rows if row["libraryID"] == library_id]
    target = clean(name).casefold()
    matches = [row["collectionID"] for row in rows if clean(row["collectionName"]).casefold() == target]
    if not matches:
        raise ValueError(f"no Zotero collection named {name!r}")

    children: dict[int, list[int]] = defaultdict(list)
    for row in rows:
        parent = row["parentCollectionID"]
        if parent is not None:
            children[parent].append(row["collectionID"])

    wanted: set[int] = set()
    stack = list(matches)
    while stack:
        collection_id = stack.pop()
        if collection_id in wanted:
            continue
        wanted.add(collection_id)
        stack.extend(children.get(collection_id, ()))
    return wanted


def _sqlite_creator_to_name(row: sqlite3.Row) -> Name:
    if row["fieldMode"] == 1:
        return Name(literal=clean(row["lastName"] or ""), collective=True)
    return Name(family=clean(row["lastName"] or ""), given=clean(row["firstName"] or ""))


def _build_reference_from_row(
    item_key: str,
    type_name: str,
    fields: dict[str, str],
    creators: list[sqlite3.Row],
) -> Reference:
    author_rows = [c for c in creators if c["creatorType"] == "author"]
    role = ""
    if not author_rows:
        author_rows = [c for c in creators if c["creatorType"] == "editor"]
        role = "editor" if author_rows else ""

    container = fields.get("publicationTitle") or fields.get("bookTitle")
    raw: dict[str, Any] = {
        "itemType": type_name,
        "key": item_key,
        "fields": dict(fields),
        "creators": [dict(c) for c in creators],
    }
    if role:
        raw["creator_role"] = role

    return Reference(
        key=item_key,
        locator=f"zotero:{item_key}",
        kind=normalize_kind(type_name),
        doi=normalize_doi(fields["DOI"]) if fields.get("DOI") else None,
        isbn=clean(fields["ISBN"]) if fields.get("ISBN") else None,
        url=clean(fields["url"]) if fields.get("url") else None,
        title=clean(fields["title"]) if fields.get("title") else None,
        authors=[_sqlite_creator_to_name(c) for c in author_rows],
        year=parse_year(fields["date"]) if fields.get("date") else None,
        container=clean(container) if container else None,
        volume=clean(fields["volume"]) if fields.get("volume") else None,
        issue=clean(fields["issue"]) if fields.get("issue") else None,
        pages=clean(fields["pages"]) if fields.get("pages") else None,
        publisher=clean(fields["publisher"]) if fields.get("publisher") else None,
        raw=raw,
    )


def _extract_references(
    conn: sqlite3.Connection,
    *,
    include_trashed: bool,
    collection: str | None,
) -> list[Reference]:
    cursor = conn.cursor()

    # fieldIDs are assigned by Zotero's schema migrations and are not stable
    # across versions; resolving them by name here is what keeps this query
    # correct after a user upgrades their Zotero install. itemData's own
    # foreign key targets fieldsCombined, not fields: fields alone omits any
    # library-specific custom field a translator or plugin has defined, and
    # a value stored under one of those would otherwise vanish from `raw`
    # with no error -- reading fieldsCombined is what "resolved by name"
    # actually requires. fieldsCombined itself is a schema addition (Zotero
    # 5.0.71, 2019); a database from before it existed has no custom fields
    # to lose, so falling back to the older, always-present fields table
    # keeps this working there too instead of raising.
    try:
        field_rows = cursor.execute("SELECT fieldID, fieldName FROM fieldsCombined").fetchall()
    except sqlite3.OperationalError:
        field_rows = cursor.execute("SELECT fieldID, fieldName FROM fields").fetchall()
    field_names: dict[int, str] = {row["fieldID"]: row["fieldName"] for row in field_rows}

    item_fields: dict[int, dict[str, str]] = defaultdict(dict)
    for row in cursor.execute(
        "SELECT itemData.itemID AS itemID, itemData.fieldID AS fieldID, itemDataValues.value AS value "
        "FROM itemData JOIN itemDataValues ON itemDataValues.valueID = itemData.valueID"
    ):
        name = field_names.get(row["fieldID"])
        if name is not None:
            item_fields[row["itemID"]][name] = row["value"]

    item_creators: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in cursor.execute(
        "SELECT itemCreators.itemID AS itemID, creatorTypes.creatorType AS creatorType, "
        "creators.firstName AS firstName, creators.lastName AS lastName, creators.fieldMode AS fieldMode "
        "FROM itemCreators "
        "JOIN creators ON creators.creatorID = itemCreators.creatorID "
        "JOIN creatorTypes ON creatorTypes.creatorTypeID = itemCreators.creatorTypeID "
        "ORDER BY itemCreators.itemID, itemCreators.orderIndex"
    ):
        item_creators[row["itemID"]].append(row)

    # A synced account has one personal library plus one row per group it
    # belongs to. Zotero item keys and collection names are unique only
    # *within* a library, so leaving this unscoped can silently merge a
    # group library's items into the personal one under colliding keys and
    # match a same-named collection in the wrong library entirely -- see
    # _personal_library_id.
    library_id = _personal_library_id(cursor)

    wanted_collection_ids = (
        _resolve_collection_ids(cursor, collection, library_id) if collection is not None else None
    )

    item_collection_ids: dict[int, set[int]] = defaultdict(set)
    if wanted_collection_ids is not None:
        for row in cursor.execute("SELECT collectionID, itemID FROM collectionItems"):
            item_collection_ids[row["itemID"]].add(row["collectionID"])

    trashed_item_ids: set[int] = set()
    if not include_trashed:
        trashed_item_ids = {row["itemID"] for row in cursor.execute("SELECT itemID FROM deletedItems")}

    skip_types = ",".join("?" for _ in _NON_BIBLIOGRAPHIC_ITEM_TYPES)
    library_clause = " AND items.libraryID = ?" if library_id is not None else ""
    params: tuple[object, ...] = tuple(_NON_BIBLIOGRAPHIC_ITEM_TYPES)
    if library_id is not None:
        params += (library_id,)
    references: list[Reference] = []
    for row in cursor.execute(
        "SELECT items.itemID AS itemID, items.key AS itemKey, itemTypes.typeName AS typeName "
        "FROM items JOIN itemTypes ON itemTypes.itemTypeID = items.itemTypeID "
        f"WHERE itemTypes.typeName NOT IN ({skip_types}){library_clause}",
        params,
    ):
        item_id = row["itemID"]
        if item_id in trashed_item_ids:
            continue
        if wanted_collection_ids is not None and not (item_collection_ids.get(item_id, set()) & wanted_collection_ids):
            continue
        references.append(
            _build_reference_from_row(row["itemKey"], row["typeName"], item_fields.get(item_id, {}), item_creators.get(item_id, []))
        )

    # Only when the scoping actually excluded something. With library_id None
    # every library was read, so there is nothing the user was not told about.
    if library_id is not None:
        _warn_about_unread_group_libraries(
            cursor, library_id=library_id, trashed_item_ids=trashed_item_ids
        )
    return references


def _read_sqlite(path: pathlib.Path, *, include_trashed: bool, collection: str | None) -> list[Reference]:
    db_path, cleanup_dir = _snapshot_if_mid_write(path)
    # The cleanup guard has to wrap sqlite3.connect(), not just the queries:
    # connect() raises eagerly on a database it cannot open, and a snapshot
    # abandoned there is a complete, readable copy of the user's private
    # bibliography sitting in the system temp directory forever.
    try:
        conn = sqlite3.connect(_sqlite_uri(db_path), uri=True)
        try:
            conn.row_factory = sqlite3.Row
            # Belt-and-suspenders alongside immutable=1: a bug that slips a
            # write statement into this module should fail loudly, not
            # corrupt the user's live library.
            conn.execute("PRAGMA query_only = 1")
            return _extract_references(conn, include_trashed=include_trashed, collection=collection)
        finally:
            conn.close()
    finally:
        if cleanup_dir is not None:
            shutil.rmtree(cleanup_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# Zotero's local API ("local")
# --------------------------------------------------------------------------


def _local_api_call(path: str, params: dict[str, str] | None = None) -> Any:
    query = urllib.parse.urlencode(params or {})
    url = f"{_LOCAL_API_BASE}{path}" + (f"?{query}" if query else "")
    # Zotero's local server rejects requests lacking this header — added so
    # an arbitrary web page cannot read a user's library with a background
    # fetch() just because Zotero happens to be running. It costs nothing to
    # send on every call, including the ones that might not require it.
    request = urllib.request.Request(url, headers={"Zotero-Allowed-Request": "true"})
    try:
        with urllib.request.urlopen(request, timeout=_LOCAL_API_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"the local Zotero API at {url} returned HTTP {exc.code} ({exc.reason})"
        ) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise RuntimeError(
            f"could not reach the local Zotero API at {url} ({exc}). Start "
            "Zotero and make sure 'Allow other applications on this "
            "computer to communicate with Zotero' is on (Edit/Zotero > "
            "Settings > Advanced), or pass a zotero.sqlite path or an "
            "exported .json file to read_zotero() instead."
        ) from exc


def _local_api_get_all(path: str) -> list[dict[str, Any]]:
    """Every item under *path*, following the API's start/limit pagination."""
    items: list[dict[str, Any]] = []
    start = 0
    while True:
        page = _local_api_call(path, {"start": str(start), "limit": str(_LOCAL_API_PAGE_SIZE), "format": "json"})
        if not isinstance(page, list):
            raise RuntimeError(f"unexpected response shape from local Zotero API at {path}: {page!r}")
        items.extend(page)
        if len(page) < _LOCAL_API_PAGE_SIZE:
            return items
        start += _LOCAL_API_PAGE_SIZE


def _resolve_local_collection_keys(name: str) -> set[str]:
    collections = _local_api_get_all("/collections")
    by_key = {c["data"]["key"]: c["data"] for c in collections if isinstance(c.get("data"), dict)}
    target = clean(name).casefold()
    matches = [key for key, data in by_key.items() if clean(data.get("name", "")).casefold() == target]
    if not matches:
        raise ValueError(f"no Zotero collection named {name!r}")

    children: dict[str, list[str]] = defaultdict(list)
    for key, data in by_key.items():
        parent = data.get("parentCollection")
        if parent:
            children[parent].append(key)

    wanted: set[str] = set()
    stack = list(matches)
    while stack:
        key = stack.pop()
        if key in wanted:
            continue
        wanted.add(key)
        stack.extend(children.get(key, ()))
    return wanted


def _read_local(*, include_trashed: bool, collection: str | None) -> list[Reference]:
    items = _local_api_get_all("/items")
    if include_trashed:
        items += _local_api_get_all("/items/trash")

    wanted_collection_keys = _resolve_local_collection_keys(collection) if collection is not None else None

    references: list[Reference] = []
    for item in items:
        data = item.get("data", item)
        if not isinstance(data, dict):
            continue
        if data.get("itemType") in _NON_BIBLIOGRAPHIC_ITEM_TYPES:
            continue
        if wanted_collection_keys is not None and not (set(data.get("collections", [])) & wanted_collection_keys):
            continue
        references.append(_zotero_json_item_to_reference(data))
    return references
