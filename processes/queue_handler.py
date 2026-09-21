"""Module to handle queue population"""

import asyncio
import json
import logging
import os
from itertools import groupby

import pyodbc
from automation_server_client import Workqueue
from mbu_rpa_core.database.connection import RPAConnection
from mbu_rpa_core.exceptions import ProcessError

from helpers import config

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


def _fetch_addresses_for_cprs(cprs: list[str]) -> dict[str, str]:
    """
    Batch-resolve AdresseId for a list of CPRs from [LOIS].[CPR].[PersonGeoView],
    using a single ``IN (...)`` query.

    Args:
        cprs: List of CPR numbers (PNR_0) to resolve. Duplicates are fine.

    Returns:
        dict mapping CPR -> adresse_id (str). CPRs missing from PersonGeoView
        are omitted from the result.
    """
    if not cprs:
        return {}

    unique_cprs = list(set(cprs))

    lois_conn_string = os.getenv("DBCONNECTIONSTRINGSERVER29", "")
    if not lois_conn_string:
        raise ProcessError("DBCONNECTIONSTRINGSERVER29 must be set in the environment / .env file.")

    with pyodbc.connect(lois_conn_string) as conn:
        cursor = conn.cursor()

        placeholders = ", ".join("?" for _ in unique_cprs)
        cursor.execute(
            f"""
            SELECT [PNR_0], [AdresseId]
            FROM [LOIS].[CPR].[PersonGeoView]
            WHERE PNR_0 IN ({placeholders})
            """,
            unique_cprs,
        )
        cpr_to_adresse_id = {
            pnr: str(adresse_id)
            for pnr, adresse_id in cursor.fetchall()
            if adresse_id is not None
        }

    missing_cprs = set(unique_cprs) - set(cpr_to_adresse_id)
    if missing_cprs:
        logger.warning(
            "No AdresseId found in PersonGeoView for %d CPR(s): %s\n",
            len(missing_cprs),
            sorted(missing_cprs),
        )

    return cpr_to_adresse_id


def retrieve_items_for_queue() -> list[dict]:
    """
    Read befordring rows from [RPA].[rpa].[BefordringsData] and group them
    into queue items by CaseID.

    One queue item per PPR case — all bevilling rows for that case are
    embedded in the payload.  The ATS reference is the PPR case ID so
    re-running the queue phase never creates duplicates.
    """

    with RPAConnection(db_env="PROD") as rpa_conn:
        db_conn_string = rpa_conn.get_constant("DbConnectionString")["value"]

    # 🧪 TEST MODE: TOP (30) limits the run to a small batch of cases for
    # address-resolution and data-insertion testing.
    query = """
        SELECT TOP (10)
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

    # --- Resolve adresse_id for every distinct student up front ---
    unique_cprs = list({r.get("CPR", "") for r in rows if r.get("CPR")})
    cpr_to_adresse_id = _fetch_addresses_for_cprs(unique_cprs)
    logger.info(
        "Resolved adresse_id for %d/%d student(s) via LOIS.\n",
        len(cpr_to_adresse_id),
        len(unique_cprs),
    )

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

            bevillinger.append({
                **bevilling_data,
                "koerselsraekker": koerselsraekker,
            })

        items.append({
            "reference": ppr_case_id,
            "data": {
                "ppr_case_id": ppr_case_id,
                "person_ssn": person_ssn,
                "adresse_id": cpr_to_adresse_id.get(person_ssn),
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
