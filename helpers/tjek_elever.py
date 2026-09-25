"""Standalone check: are the students in BefordringsData known to us?

    python -m helpers.tjek_elever

Not part of the conversion. It writes nothing anywhere and calls no mutating
endpoint — it reads three sources and reports where they disagree, so the
gaps are known BEFORE a conversion rather than discovered one failed case at
a time.

For every distinct CPR in BefordringsData — over the same two-year window
the conversion reads, so the two agree on which students are in scope — it
answers two questions:

    in Elev?    The Elev table in Befordringssystemet, read directly where
                DBCONNECTIONSTRINGBEFORDRING is set — two chunked queries
                rather than a few thousand HTTP calls. Without it, the same
                answer via /citizen/stamdata/{cpr}, one request per CPR.
                A miss means every bevilling for that student is rejected:
                Bevilling.cpr_elev is a trusted foreign key, so the row
                cannot be created at all.

    in LOIS?    LOIS.CPR.PersonGeoView on server 29, the same view the
                nightly run uses. A miss means no fallback address, so a klub
                row or a closed case with an unresolvable address has nothing
                to fall back on. Status_T is reported alongside: CPR's own
                status text, which is often what explains a row that looks
                wrong for no visible reason.

The two are independent and fail differently, which is why they are reported
separately rather than as one "known" flag.

Environment: DBCONNECTIONSTRINGSERVER29 for LOIS and RPAConnection for
BefordringsData, both as the conversion uses them. For the Elev check,
either DBCONNECTIONSTRINGBEFORDRING (fast) or API_ENDPOINT + API_KEY (slow
fallback); --api forces the latter.
"""

import argparse
import csv
import logging
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pyodbc
import requests
from mbu_rpa_core.database.connection import RPAConnection

logger = logging.getLogger(__name__)

FELTER = (
    "cpr",
    "i_elev",
    "i_lois",
    "lois_status",
    "lois_adresse_id",
    "antal_raekker",
    "ppr_sager",
    "vurdering",
)


def _cpr_cifre(cpr) -> str:
    """The ten digits of a CPR, or "" — the form PersonGeoView keys on."""

    cifre = "".join(c for c in str(cpr or "") if c.isdigit())

    return cifre if len(cifre) == 10 else ""


def _hent_cprs() -> tuple[dict[str, int], dict[str, set[str]], int]:
    """Distinct CPRs in BefordringsData, with row counts and PPR cases.

    Limited to the last two years of bevilling dates, matching the
    conversion's own query. Note that this also drops rows with NULL dates,
    since NULL compares as unknown — the same rows the conversion no longer
    sees either.

    Also returns how many rows carried no usable CPR at all: those can never
    be converted, and they are invisible in a per-CPR report.
    """

    with RPAConnection(db_env="PROD") as rpa_conn:
        conn_string = rpa_conn.get_constant("DbConnectionString")["value"]

    with pyodbc.connect(conn_string) as conn:
        cursor = conn.cursor()

        # The SAME window the conversion uses — see the query in
        # processes/queue_handler.py. A report over rows the conversion will
        # never read would list students nobody is going to convert, and the
        # two must be changed together or this stops answering the question
        # it is asked.
        cursor.execute(
            """
            SELECT [CPR], [CaseID]
            FROM   [RPA].[rpa].[BefordringsData]
            WHERE  [BevillingFra] >= DATEADD(YEAR, -2, GETDATE()) AND
                   [BevillingTil] >= DATEADD(YEAR, -2, GETDATE())
            """
        )
        rows = cursor.fetchall()

    antal: dict[str, int] = defaultdict(int)
    sager: dict[str, set[str]] = defaultdict(set)
    uden_cpr = 0

    for cpr, sag in rows:
        rent = _cpr_cifre(cpr)

        if not rent:
            uden_cpr += 1
            continue

        antal[rent] += 1

        if sag:
            sager[rent].add(str(sag).strip())

    return dict(antal), dict(sager), uden_cpr


def _lois_status(cprs: list[str], chunk: int = 900) -> dict[str, dict]:
    """cpr -> {"adresse_id", "status"} for everyone PersonGeoView knows.

    A CPR absent from the result is absent from the view. Note the difference
    from the conversion's own lookup, which filters AdresseId IS NOT NULL:
    here a person known to CPR but without a resolved address is a distinct
    and useful case, so the filter is deliberately left off.

    Status_T is CPR's own status text. Carried through verbatim rather than
    interpreted — it is the column that explains an otherwise puzzling row,
    where a student is in the view but something about them is not ordinary.
    """

    conn_string = os.getenv("DBCONNECTIONSTRINGSERVER29")

    if not conn_string:
        logger.warning(
            "DBCONNECTIONSTRINGSERVER29 is not set — the LOIS column will be "
            "blank. Set it to the same value rpa-befordring-nightly-runs uses."
        )

        return {}

    fundet: dict[str, dict] = {}

    with pyodbc.connect(conn_string) as conn:
        cursor = conn.cursor()

        for offset in range(0, len(cprs), chunk):
            batch = cprs[offset:offset + chunk]
            placeholders = ",".join("?" for _ in batch)

            cursor.execute(
                f"""
                SELECT [PNR_0], CONVERT(NVARCHAR(36), [AdresseId]), [Status_T]
                FROM   [LOIS].[CPR].[PersonGeoView]
                WHERE  [PNR_0] IN ({placeholders})
                """,
                batch,
            )

            for pnr, adresse_id, status in cursor.fetchall():
                rent = _cpr_cifre(pnr)

                if rent:
                    fundet[rent] = {
                        "adresse_id": str(adresse_id).strip() if adresse_id else "",
                        "status": str(status).strip() if status else "",
                    }

            logger.info("LOIS: %d/%d checked", min(offset + chunk, len(cprs)), len(cprs))

    return fundet


def _elev_status_db(cprs: list[str], chunk: int = 900) -> dict[str, bool] | None:
    """cpr -> in Elev, read straight from Befordringssystemet. None if no DSN.

    Two chunked queries instead of a few thousand HTTP round trips, which is
    the difference between seconds and minutes. Same variable the nightly run
    uses, so a machine running both RPAs already has it.

    Direct SQL rather than the API is a deliberate exception for this script.
    The conversion itself is API-only on purpose; a read-only diagnostic that
    already queries LOIS directly has no such constraint, and there is no
    bulk endpoint to use instead.

    Chunked at 900 for the same reason as everywhere else: SQL Server caps a
    statement at 2100 parameters.
    """

    conn_string = os.getenv("DBCONNECTIONSTRINGBEFORDRING")

    if not conn_string:
        return None

    fundet: set[str] = set()

    with pyodbc.connect(conn_string) as conn:
        cursor = conn.cursor()

        for offset in range(0, len(cprs), chunk):
            batch = cprs[offset:offset + chunk]
            placeholders = ",".join("?" for _ in batch)

            cursor.execute(
                f"SELECT [cpr] FROM [befordring].[Elev] "
                f"WHERE [cpr] IN ({placeholders})",
                batch,
            )

            fundet.update(_cpr_cifre(row[0]) for row in cursor.fetchall())

            logger.info("Elev: %d/%d checked", min(offset + chunk, len(cprs)), len(cprs))

    return {cpr: cpr in fundet for cpr in cprs}


def _elev_status(cprs: list[str], workers: int = 8) -> dict[str, bool]:
    """cpr -> whether the befordring API knows the student.

    The fallback, used when DBCONNECTIONSTRINGBEFORDRING is not set. One
    request each, because the API has no bulk endpoint. Run in a small thread
    pool: a few thousand sequential round trips is minutes of waiting for no
    reason, and eight at a time is gentle on the API.

    A request that FAILS is recorded as None rather than False, so a network
    blip cannot be read as "this student does not exist".
    """

    api_endpoint = os.getenv("API_ENDPOINT", "")
    api_key = os.getenv("API_KEY", "")

    if not api_endpoint or not api_key:
        raise SystemExit("API_ENDPOINT and API_KEY must be set in the environment.")

    headers = {"X-API-Key": api_key}
    session = requests.Session()

    def tjek(cpr: str):
        try:
            response = session.get(
                f"{api_endpoint}/citizen/stamdata/{cpr}",
                headers=headers,
                timeout=30,
            )

            if not response.ok:
                return cpr, None

            return cpr, response.json() is not None
        except requests.RequestException:
            return cpr, None

    resultat: dict[str, bool] = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (cpr, findes) in enumerate(pool.map(tjek, cprs), start=1):
            resultat[cpr] = findes

            if i % 200 == 0:
                logger.info("Elev: %d/%d checked", i, len(cprs))

    return resultat


def _vurdering(i_elev, i_lois: bool) -> str:
    """One line saying what the combination means for a conversion."""

    if i_elev is None:
        return "UKENDT — opslaget mod Elev fejlede, prøv igen"

    if i_elev and i_lois:
        return "ok"

    if i_elev and not i_lois:
        return "mangler i LOIS — ingen reserveadresse ved klub/lukket sag"

    if not i_elev and i_lois:
        return "MANGLER I ELEV — alle bevillinger afvises"

    return "MANGLER I BEGGE — alle bevillinger afvises"


def _skriv(raekker: list[dict], stamme: str) -> None:
    """Write the report as CSV and, where openpyxl is available, as xlsx."""

    csv_sti = Path(f"{stamme}.csv")

    with csv_sti.open("w", newline="", encoding="utf-8-sig") as fil:
        writer = csv.DictWriter(fil, fieldnames=FELTER)
        writer.writeheader()
        writer.writerows(raekker)

    print(f"Skrev {len(raekker)} række(r) til {csv_sti}")

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
        from openpyxl.utils import get_column_letter
    except ImportError:
        print("openpyxl mangler — kun CSV blev skrevet.")

        return

    xlsx_sti = Path(f"{stamme}.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = "Elevtjek"
    ws.append(list(FELTER))

    for celle in ws[1]:
        celle.font = Font(bold=True)

    for raekke in raekker:
        ws.append([raekke[felt] for felt in FELTER])

    bredder = {"cpr": 14, "i_elev": 10, "i_lois": 10, "lois_status": 22,
               "lois_adresse_id": 38, "antal_raekker": 14, "ppr_sager": 46,
               "vurdering": 52}

    for i, felt in enumerate(FELTER, start=1):
        ws.column_dimensions[get_column_letter(i)].width = bredder.get(felt, 18)

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(xlsx_sti)

    print(f"Skrev de samme rækker til {xlsx_sti}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0,
                        help="kun de første N CPR'er — til en hurtig prøve")
    parser.add_argument("--workers", type=int, default=8,
                        help="samtidige API-kald mod /citizen/stamdata")
    parser.add_argument("--ud", default="elevtjek",
                        help="filnavn uden endelse")
    parser.add_argument("--api", action="store_true",
                        help="tjek Elev via API'et i stedet for direkte "
                             "databaseopslag, selv hvis forbindelsen findes")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    antal, sager, uden_cpr = _hent_cprs()
    cprs = sorted(antal)

    if args.limit:
        cprs = cprs[:args.limit]

    print(f"{len(antal)} distinkte CPR-numre i BefordringsData"
          f"{f' — tjekker de første {len(cprs)}' if args.limit else ''}")

    if uden_cpr:
        print(f"ADVARSEL: {uden_cpr} række(r) har intet brugbart CPR og kan aldrig konverteres")

    lois = _lois_status(cprs)

    elev = None if args.api else _elev_status_db(cprs)

    if elev is None:
        print("Elev slås op via API'et, ét kald pr. CPR — det tager et stykke tid."
              if args.api else
              "DBCONNECTIONSTRINGBEFORDRING er ikke sat — falder tilbage til "
              "API'et med ét kald pr. CPR. Sæt den for at gøre det væsentligt "
              "hurtigere.")
        elev = _elev_status(cprs, workers=args.workers)

    raekker = []

    for cpr in cprs:
        i_elev = elev.get(cpr)
        i_lois = cpr in lois

        oplysninger = lois.get(cpr) or {}

        raekker.append({
            "cpr": cpr,
            "i_elev": {True: "ja", False: "nej", None: "?"}[i_elev],
            "i_lois": "ja" if i_lois else "nej",
            "lois_status": oplysninger.get("status", ""),
            "lois_adresse_id": oplysninger.get("adresse_id", ""),
            "antal_raekker": antal[cpr],
            "ppr_sager": "; ".join(sorted(sager.get(cpr, ()))),
            "vurdering": _vurdering(i_elev, i_lois),
        })

    # Worst first: the rows someone has to act on should not be buried.
    raekker.sort(key=lambda r: (r["vurdering"] == "ok", r["vurdering"], r["cpr"]))

    print()
    print(f"  i Elev            : {sum(1 for r in raekker if r['i_elev'] == 'ja')}")
    print(f"  IKKE i Elev       : {sum(1 for r in raekker if r['i_elev'] == 'nej')}")
    print(f"  opslag fejlede    : {sum(1 for r in raekker if r['i_elev'] == '?')}")
    print(f"  i LOIS            : {sum(1 for r in raekker if r['i_lois'] == 'ja')}")
    print(f"  IKKE i LOIS       : {sum(1 for r in raekker if r['i_lois'] == 'nej')}")
    print(f"  uden adresse i LOIS: "
          f"{sum(1 for r in raekker if r['i_lois'] == 'ja' and not r['lois_adresse_id'])}")

    # Every Status_T that turned up, with counts. Printed rather than judged:
    # the values are CPR's, not ours, and seeing the spread is the point.
    statusser: dict[str, int] = defaultdict(int)

    for r in raekker:
        if r["i_lois"] == "ja":
            statusser[r["lois_status"] or "(tom)"] += 1

    if statusser:
        print()
        print("  Status_T i LOIS:")

        for status, n in sorted(statusser.items(), key=lambda s: (-s[1], s[0])):
            print(f"    {n:6}  {status}")

    print()

    _skriv(raekker, args.ud)


if __name__ == "__main__":
    sys.exit(main())
