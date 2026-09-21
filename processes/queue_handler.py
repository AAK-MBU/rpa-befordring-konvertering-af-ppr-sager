"""Module to handle queue population"""

import asyncio
import json
import logging
import re
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

    items = []

    for ppr_case_id, case_iter in groupby(rows, key=lambda r: r["CaseID"]):
        case_rows = list(case_iter)
        person_ssn = case_rows[0].get("CPR", "")

        # Within each case, group rows into bevillinger by (BevillingFra, BevillingTil).
        # Rows sharing the same date pair are kørselsrækker under the same bevilling.
        # Rows with different date pairs are separate (e.g. outdated) bevillinger.
        bevillinger = []
        for bev_key, bev_iter in groupby(case_rows, key=lambda r: (r["BevillingFra"], r["BevillingTil"])):
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

            # Kørselsrække-level fields — one entry per row
            koerselsraekker = [
                {col: _serialize(val) for col, val in row.items() if col in _KOERSELSRAEKKE_FIELDS}
                for row in bev_rows
            ]

            # The address belongs to the bevilling, not the case: a student
            # who moved has older bevillinger at the previous address, and
            # Bevilling.adresse_id is meant to record where each one was
            # granted. Resolved per bevilling for that reason.
            #
            # Rows within one bevilling can still disagree, so take the first
            # that resolves rather than trusting row zero.
            bevilling_adresse_id = next(
                (
                    adresse_ids[key]
                    for key in (_address_key(row) for row in bev_rows)
                    if key and key in adresse_ids
                ),
                None,
            )

            bevillinger.append({
                **bevilling_data,
                "adresse_id": bevilling_adresse_id,
                "koerselsraekker": koerselsraekker,
            })

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
