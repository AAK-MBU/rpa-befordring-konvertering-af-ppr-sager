"""Module to handle queue population"""

import asyncio
import calendar
import json
import logging
import re
from datetime import date
from itertools import groupby

import pyodbc
import requests
from automation_server_client import Workqueue
from mbu_rpa_core.database.connection import RPAConnection
from mbu_rpa_core.exceptions import ProcessError

from helpers import config
from processes.bevilling_creation import get_api_credentials

logger = logging.getLogger(__name__)


class RequestError(Exception):
    """Custom exception for request related errors."""


def _serialize(value) -> str | None:
    """Convert a DB value to a JSON-safe type (handles dates, None)."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


# Fields that belong to the kørselsrække (one per row).
# Everything else in a BefordringsData row is bevilling-level data,
# shared across all kørselsrækker within the same bevilling.
_KOERSELSRAEKKE_FIELDS = {
    "BevillingFra",
    "BevillingTil",
    "TidspunktForBevilling",
    "BevillingAfKoerselstype",
    "BevilgetKoereAfstand",
    "Kommentar",
}


_POSTCODE = re.compile(r"^(\d{4})\b")


def _components(tekst: str | None) -> list[str]:
    """Split an address into normalised comma-separated parts.

    Whitespace runs are collapsed and everything is casefolded, so
    "Kærlundvej  16" and "KÆRLUNDVEJ 16" compare equal.
    """

    return [
        " ".join(part.split()).casefold()
        for part in str(tekst or "").split(",")
        if part.strip()
    ]


def _postcode_of(components: list[str]) -> str:
    """The four-digit postcode from the last component, or ''."""

    if not components:
        return ""

    match = _POSTCODE.match(components[-1])

    return match.group(1) if match else ""


def _matches(candidate_tekst: str | None, source: list[str]) -> bool:
    """Does a register address describe the same place as the source address?

    The two systems write the middle of an address differently, and the
    difference is not one thing:

        supplementary place name, only in the register
            BefordringsData   Kærlundvej 16, 8260 Viby J
            Adresse           Kærlundvej 16, Ormslev, 8260 Viby J

        floor and door, in both — and load-bearing
            BefordringsData   Langkærvej 19, st. tv, 8381 Tilst
            Adresse           Langkærvej 19, st. tv, 8381 Tilst
                              Langkærvej 19, st. th, 8381 Tilst   <- different flat
                              Langkærvej 19, 1. tv, 8381 Tilst    <- different flat

    Classifying each middle part as "place name" or "floor/door" would mean
    parsing Danish address conventions and getting them right for every
    variant. Subsequence matching sidesteps that: the street must be equal,
    the postcode must be equal, and every middle part of the SOURCE must
    appear among the candidate's middle parts in the same order.

    Extra parts in the candidate are therefore fine — that is the place name
    the legacy data never had. Missing ones are not — that is a different flat.

    A source with no floor/door still matches all of them, which is correct:
    nothing in the data says which flat, so the caller sees several candidates
    and refuses rather than guessing.
    """

    candidate = _components(candidate_tekst)

    if len(candidate) < 2 or len(source) < 2:
        return False

    if candidate[0] != source[0]:
        return False

    if _postcode_of(candidate) != _postcode_of(source):
        return False

    # Every middle part of the source, in order, somewhere in the candidate's.
    remaining = iter(candidate[1:-1])

    return all(part in remaining for part in source[1:-1])


def _address_key(row: dict) -> tuple[str, ...] | None:
    """The normalised components of the address a row's bevilling is for.

    Not the student's current address — that already sits on Elev, put there
    by the nightly run, and nothing here needs to look it up. This is the
    address the legacy bevilling was granted against, which becomes
    Bevilling.adresse_id and is what adresse_mismatch later compares to the
    student's own.

    ElevensAdresse already carries the postcode and city, so ElevensPostnummer
    is only a fallback for a row whose address string is missing it.
    """

    components = _components(row.get("ElevensAdresse"))

    if not components:
        return None

    if not _postcode_of(components):
        postnummer = str(row.get("ElevensPostnummer") or "").strip()

        if not postnummer:
            return None

        components = components + [postnummer]

    if len(components) < 2:
        return None

    return tuple(components)


def _search_prefixes(source: list[str]) -> list[str]:
    """Search prefixes to try for an address, most selective first.

    Every adresse_tekst has a comma straight after the house number, so
    "Langkærvej 19," matches "Langkærvej 19, st. tv, ..." but not
    "Langkærvej 190, ..." or "Langkærvej 19A, ...". That comma is what makes a
    prefix search safe at all.

    Where the source names a floor and door, that is included too:
    /adresse/search returns at most 15 rows, and a block of flats can easily
    exceed that on the street prefix alone — the wanted address would be
    pushed out of the results and look absent. The street-only prefix is kept
    as a fallback, because the register sometimes puts a supplementary place
    name where this assumes the floor is.
    """

    street = source[0]

    if len(source) > 2:
        return [f"{street}, {source[1]},", f"{street},"]

    return [f"{street},"]


def _resolve_adresse_ids(rows: list[dict]) -> dict[tuple[str, ...], str]:
    """Resolve each distinct bevilling address to an adresse_id.

    Resolved against the befordring application's own Adresse table, through
    its API. That table is a full copy of the municipality's address register,
    refreshed nightly by rpa-befordring-nightly-runs from
    LOIS.DAR.AdresseDkGeoView — so there is no longer any reason for this bot
    to reach into LOIS on server 29 itself, and it keeps the conversion
    API-only rather than half API, half direct database.

    Accepted only when exactly one candidate matches. Placing a bevilling at
    the wrong address is worse than failing to place it.

    Args:
        rows: BefordringsData rows.

    Returns:
        dict mapping the address components -> adresse_id. Addresses that
        resolve to nothing, or to more than one candidate, are omitted and
        logged; the bevilling using them is then rejected for manual
        follow-up, since Bevilling.adresse_id is NOT NULL.
    """

    api_endpoint, api_key = get_api_credentials()
    headers = {"X-API-Key": api_key}

    keys = {key for key in (_address_key(row) for row in rows) if key}

    resolved: dict[tuple[str, ...], str] = {}
    unresolved: list[tuple[tuple[str, ...], int]] = []

    for key in sorted(keys):
        source = list(key)
        candidates: list[dict] = []

        for prefix in _search_prefixes(source):
            response = requests.get(
                f"{api_endpoint}/adresse/search",
                params={"q": prefix},
                headers=headers,
                timeout=30,
            )

            if not response.ok:
                raise ProcessError(
                    f"Address search failed for {prefix!r}: "
                    f"{response.status_code} — {response.text}"
                )

            candidates = [
                candidate
                for candidate in (response.json() or [])
                if _matches(candidate.get("adresse_tekst"), source)
            ]

            if candidates:
                break

        if len(candidates) == 1:
            resolved[key] = candidates[0]["adresse_id"]
            continue

        unresolved.append((key, len(candidates)))

    logger.info(
        "Resolved %d/%d distinct bevilling address(es) against the Adresse table.\n",
        len(resolved),
        len(keys),
    )

    if unresolved:
        logger.warning(
            "%d address(es) unresolved — the bevillinger using them will be "
            "rejected for manual follow-up:\n%s\n",
            len(unresolved),
            "\n".join(
                f"  {', '.join(key)} — {count} candidate(s)"
                for key, count in unresolved
            ),
        )

    return resolved


# Places whose appearance in ElevensAdresse or SkoleNavnBefordring means the
# row is really about club transport — see _klub_note.
_KLUB_MARKERS: tuple[str, ...] = (
    "Klubben Holme Søndergård",
)


def _fold(value: str | None) -> str:
    """Casefold, strip spaces, and flatten æ/ø/å for a tolerant contains-match.

    "Søndergård" and "Søndergaard" are the same place written two ways, and
    the source spells it both. Folding both sides means the marker matches
    either without listing every spelling.
    """

    folded = "".join(str(value or "").split()).casefold()

    for special, plain in (("æ", "ae"), ("ø", "oe"), ("å", "aa")):
        folded = folded.replace(special, plain)

    return folded


def _klub_note(row: dict) -> str | None:
    """A note preserving the raw address and school, when a row means "klub".

    Befordring to and from a klub does not exist in the old system, but does
    here. To record it anyway, caseworkers put the klub in ElevensAdresse and
    the student's home in SkoleNavnBefordring — or the reverse for the return
    trip. The row therefore describes a journey its own columns misname, and
    no automatic conversion can recover which was which.

    So the values are carried across verbatim in the kørselsrække's comment.
    A caseworker reading it can see what the row actually said and rebuild the
    klub kørsel properly; without it, the only trace is a bevilling whose
    address looks wrong.
    """

    adresse = row.get("ElevensAdresse")
    skole = row.get("SkoleNavnBefordring")

    haystack = _fold(adresse) + "\x00" + _fold(skole)

    if not any(_fold(marker) in haystack for marker in _KLUB_MARKERS):
        return None

    return (
        "Fra foranstaltningsdata konvertering:\n"
        f"ElevensAdresse: {str(adresse or '').strip()}\n"
        f"SkoleNavnBefordring: {str(skole or '').strip()}"
    )


def _extend_kommentar(kommentar: str | None, note: str | None) -> str | None:
    """Append a note to a comment, keeping whichever of the two exists."""

    existing = str(kommentar or "").strip()

    if not note:
        return existing or None

    return f"{existing}\n\n{note}" if existing else note


def _one_month_on(day: date) -> date:
    """The same day one month later, clamped to the end of a shorter month."""

    year = day.year + (day.month // 12)
    month = day.month % 12 + 1

    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _as_date(value) -> date | None:
    """Parse a BefordringsData date, or None when it is absent or unreadable."""

    if value is None:
        return None

    if isinstance(value, date):
        return value

    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _bucket_key(row: dict, today: date, window_end: date) -> tuple:
    """Which bevilling a BefordringsData row belongs to, within its case.

    The old grouping put every distinct (BevillingFra, BevillingTil) on its own
    bevilling. That cannot be converted as-is: usp_recalculate_bevilling_status
    fails a citizen who ends up with more than one ACTIVE bevilling, and a
    student with two overlapping legacy rows would produce exactly that — the
    whole case lands on Fejlet.

    So rows are bucketed by when they apply rather than by their exact dates:

      ("current",)            period overlaps [today, window_end]
                              -> ONE bevilling, which the status engine will
                                 compute as Aktiv. Merging these is the point.

      ("future",)             period starts after window_end
                              -> ONE bevilling, computed as Kommende. Also
                                 merged, even where the periods are far apart:
                                 the legacy data is not detailed enough to
                                 split them into meaningful separate
                                 bevillinger, and only one may be Aktiv later.

      ("past", fra, til)      everything else — already ended
                              -> one bevilling per distinct period, as before.
                                 These compute as Udløbet, and Udløbet does not
                                 collide.

      ("ukendt", fra, til)    dates missing or unreadable
                              -> kept apart under the raw values rather than
                                 guessed into a bucket. Logged by the caller.

    Nothing here assigns a status: the status engine derives Aktiv / Kommende /
    Udløbet from the kørselsrække dates once the rows exist. This only decides
    which rows share a bevilling.
    """

    fra = _as_date(row.get("BevillingFra"))
    til = _as_date(row.get("BevillingTil"))

    if fra is None or til is None:
        return ("ukendt", str(row.get("BevillingFra")), str(row.get("BevillingTil")))

    if fra <= window_end and til >= today:
        return ("current",)

    if fra > window_end:
        return ("future",)

    return ("past", fra.isoformat(), til.isoformat())


def retrieve_items_for_queue() -> list[dict]:
    """
    Read befordring rows from [RPA].[rpa].[BefordringsData] and group them
    into queue items by CaseID.

    One queue item per PPR case — all bevilling rows for that case are
    embedded in the payload.  The ATS reference is the PPR case ID so
    re-running the queue phase never creates duplicates.

    Each BEVILLING's adresse_id is resolved up front from the befordring
    application's own Adresse table (see _resolve_adresse_ids), so the whole
    conversion talks to one system rather than reaching into LOIS separately.
    The student's own address is not resolved here at all — it is already on
    Elev, put there by the nightly run.
    """

    with RPAConnection(db_env="PROD") as rpa_conn:
        db_conn_string = rpa_conn.get_constant("DbConnectionString")["value"]

    # 🧪 TEST MODE: TOP (30) limits the run to a small batch of cases for
    # address-resolution and data-insertion testing.
    query = """
        SELECT TOP (100)
            [CaseDBID], [CaseID], [CPR], [Title], [Created], [CreationDate],
            [Author], [Modified], [ModifiedDate], [Editor], [Sagsbehandler],
            [Loadtime], [BevillingFra], [BevillingTil], [Revurdering],
            [ElevensAdresse], [ElevensPostnummer], [SkoleNavnBefordring],
            [SkoleID], [SkolensAdresse], [SkolensPostnummer],
            [KortestGaaAfstand], [BevilgetKoereAfstand], [Klasseart],
            [HjemmelForBevilling], [TidspunktForBevilling],
            [BevillingAfKoerselstype], [Kommentar]
        FROM [RPA].[rpa].[BefordringsData]
        ORDER BY [CaseID] DESC
    """

    with pyodbc.connect(db_conn_string) as conn:
        cursor = conn.cursor()
        cursor.execute(query)
        columns = [col[0] for col in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

    logger.info("Fetched %d row(s) from BefordringsData.\n", len(rows))

    # --- Resolve every distinct address up front ---
    adresse_ids = _resolve_adresse_ids(rows)

    today = date.today()
    window_end = config.CONVERSION_WINDOW_END or _one_month_on(today)

    logger.info(
        "Conversion window: %s to %s. Rows overlapping it become each "
        "student's one active bevilling.\n",
        today.isoformat(),
        window_end.isoformat(),
    )

    undated = [r for r in rows if _bucket_key(r, today, window_end)[0] == "ukendt"]

    if undated:
        logger.warning(
            "%d row(s) have missing or unreadable BevillingFra/BevillingTil. "
            "They are kept on separate bevillinger under their raw values "
            "rather than guessed into a bucket. Cases: %s\n",
            len(undated),
            ", ".join(sorted({str(r.get("CaseID")) for r in undated})),
        )

    items = []
    ambiguous_cases: list[str] = []
    klub_rows = 0

    # groupby only groups CONSECUTIVE equal keys, so both levels sort first.
    # The query orders by CaseID alone, which left the inner grouping at the
    # mercy of row order — a case whose rows ran date-pair A, B, A produced
    # three bevillinger instead of two.
    rows = sorted(rows, key=lambda r: str(r["CaseID"]))

    for ppr_case_id, case_iter in groupby(rows, key=lambda r: r["CaseID"]):
        case_rows = list(case_iter)
        person_ssn = case_rows[0].get("CPR", "")

        # Within each case, rows are bucketed by when they apply — see
        # _bucket_key for why current and future rows are merged rather than
        # split by their exact dates.
        def bucket_of(row):
            return _bucket_key(row, today, window_end)

        case_rows = sorted(case_rows, key=lambda r: (bucket_of(r), str(r.get("CaseDBID") or "")))

        bevillinger = []
        for bev_key, bev_iter in groupby(case_rows, key=bucket_of):
            bev_rows = list(bev_iter)
            first = bev_rows[0]

            # Bevilling-level fields — shared across rows in this group.
            # Most columns are identical on every row, but nullable fields like
            # Revurdering may be NULL on early rows and populated on a later one.
            # Take the first non-None value across all rows so nothing is lost.
            bevilling_data = {
                col: _serialize(
                    next((r[col] for r in bev_rows if r[col] is not None), None)
                )
                for col in first
                if col not in _KOERSELSRAEKKE_FIELDS
            }

            # Kørselsrække-level fields — one entry per row.
            #
            # The comment is extended per ROW, not per bevilling: the klub
            # marker sits on the individual row, and it is that row's journey
            # the note describes.
            koerselsraekker = []

            for row in bev_rows:
                koersel = {
                    col: _serialize(val)
                    for col, val in row.items()
                    if col in _KOERSELSRAEKKE_FIELDS
                }

                note = _klub_note(row)

                if note:
                    klub_rows += 1

                koersel["Kommentar"] = _extend_kommentar(koersel.get("Kommentar"), note)

                koerselsraekker.append(koersel)

            # The address belongs to the bevilling, not the case: a student
            # who moved has older bevillinger at the previous address, and
            # Bevilling.adresse_id is meant to record where each one was
            # granted. Resolved per bevilling for that reason.
            #
            # Rows within one bevilling can disagree — a klub row names the
            # klub where the others name the home, and a bucket spanning a move
            # holds both addresses. Which one belongs on the bevilling cannot be
            # decided here: it depends on where the student lives NOW, and only
            # bevilling_creation has the Elev record. So every distinct address
            # the rows resolve to is passed on, in row order, and the choice is
            # made there.
            bevilling_adresse_kandidater = []

            for row in bev_rows:
                key = _address_key(row)

                if not key or key not in adresse_ids:
                    continue

                kandidat = adresse_ids[key]

                if kandidat not in bevilling_adresse_kandidater:
                    bevilling_adresse_kandidater.append(kandidat)

            # The fallback, and what process_item checks to reject a case whose
            # address could not be matched at all. bevilling_creation overrides
            # it with whichever candidate matches the student's own address.
            bevilling_adresse_id = (
                bevilling_adresse_kandidater[0]
                if bevilling_adresse_kandidater
                else None
            )

            # The earliest date the bevilling's kørsel starts. Two jobs:
            # it is the value the application's own "ny bevilling" flow seeds
            # gyldig_fra from, and it is what makes a converted bevilling
            # identifiable on a re-run — every bevilling in a case shares the
            # same esdh_noegle, so (esdh_noegle, foerste_koersel_dato) is the
            # only pair that tells them apart.
            starts = [d for d in (_as_date(r.get("BevillingFra")) for r in bev_rows) if d]
            foerste_koersel_dato = min(starts).isoformat() if starts else None

            # When the bevilling was last worked on. Taken as the NEWEST
            # Modified across the bucket's rows, not the first non-None the
            # bevilling-level pass would have picked: the rows were edited at
            # different times, and the most recent one is what "last handled"
            # means. ModifiedDate is the fallback where Modified is empty.
            touched = [
                d
                for d in (
                    _as_date(r.get("Modified")) or _as_date(r.get("ModifiedDate"))
                    for r in bev_rows
                )
                if d
            ]
            sagsbehandlingsdato = max(touched).isoformat() if touched else None

            bevillinger.append({
                **bevilling_data,
                "adresse_id": bevilling_adresse_id,
                "adresse_id_kandidater": bevilling_adresse_kandidater,
                "bucket": bev_key[0],
                "foerste_koersel_dato": foerste_koersel_dato,
                "sagsbehandlingsdato": sagsbehandlingsdato,
                "koerselsraekker": koerselsraekker,
            })

        # (esdh_noegle, foerste_koersel_dato) is what tells one converted
        # bevilling from another on a re-run, and esdh_noegle is the same for
        # the whole case — so two buckets starting on the same day are
        # indistinguishable afterwards. That happens when a short bevilling
        # and a longer one begin on the same date and only one has ended.
        #
        # A clean first run converts both correctly; only a RESUME after a
        # partial failure would skip the second. Warned rather than rejected,
        # because the case is otherwise perfectly convertible — but these are
        # the cases to check by hand if the conversion ever has to be
        # restarted part-way.
        starts = [b["foerste_koersel_dato"] for b in bevillinger if b["foerste_koersel_dato"]]

        if len(set(starts)) != len(starts):
            ambiguous_cases.append(str(ppr_case_id))

        items.append({
            "reference": ppr_case_id,
            "data": {
                "ppr_case_id": ppr_case_id,
                "person_ssn": person_ssn,
                "bevillinger": bevillinger,
            },
        })

    logger.info(
        "Grouped into %d queue item(s) by PPR case ID.\n",
        len(items),
    )

    if klub_rows:
        logger.info(
            "%d kørselsrække(r) mention a klub in ElevensAdresse or "
            "SkoleNavnBefordring. The old system had no klub kørsel, so those "
            "columns were used to stand in for it — the raw values are "
            "carried across in the comment for a caseworker to rebuild "
            "from.\n",
            klub_rows,
        )

    if ambiguous_cases:
        logger.warning(
            "%d case(s) have two or more bevillinger starting on the same "
            "date, so they cannot be told apart on a resumed run. They "
            "convert correctly on a clean run; check these by hand if the "
            "conversion is ever restarted part-way:\n%s\n",
            len(ambiguous_cases),
            "\n".join(f"  {case}" for case in sorted(ambiguous_cases)),
        )

    return items


def create_sort_key(item: dict) -> str:
    """
    Create a sort key based on the entire JSON structure.
    Converts the item to a sorted JSON string for consistent ordering.
    """
    return json.dumps(item, sort_keys=True, ensure_ascii=False)


async def concurrent_add(workqueue: Workqueue, items: list[dict]) -> None:
    """
    Populate the workqueue with items to be processed.
    Uses concurrency and retries with exponential backoff.

    Args:
        workqueue (Workqueue): The workqueue to populate.
        items (list[dict]): List of items to add to the queue.

    Returns:
        None

    Raises:
        Exception: If adding an item fails after all retries.
    """
    sem = asyncio.Semaphore(config.MAX_CONCURRENCY)

    async def add_one(it: dict):
        reference = str(it.get("reference") or "")
        data = {"item": it}

        async with sem:
            for attempt in range(1, config.MAX_RETRIES + 1):
                try:
                    await asyncio.to_thread(workqueue.add_item, data, reference)
                    logger.info("Added item to queue with reference: %s", reference)
                    return True

                except Exception as e:
                    if attempt >= config.MAX_RETRIES:
                        logger.error(
                            "Failed to add item %s after %d attempts: %s",
                            reference,
                            attempt,
                            e,
                        )
                        return False

                    backoff = config.RETRY_BASE_DELAY * (2 ** (attempt - 1))

                    logger.warning(
                        "Error adding %s (attempt %d/%d). Retrying in %.2fs... %s",
                        reference,
                        attempt,
                        config.MAX_RETRIES,
                        backoff,
                        e,
                    )
                    await asyncio.sleep(backoff)

    if not items:
        logger.info("No new items to add.")
        return

    sorted_items = sorted(items, key=create_sort_key)
    logger.info(
        "Processing %d items sorted by complete JSON structure", len(sorted_items)
    )

    results = await asyncio.gather(*(add_one(i) for i in sorted_items))
    successes = sum(1 for r in results if r)
    failures = len(results) - successes

    logger.info(
        "Summary: %d succeeded, %d failed out of %d", successes, failures, len(results)
    )
