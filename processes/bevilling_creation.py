"""
Bevilling flow: create bevilling and koerselsraekke records in the
befordring application via its REST API.
"""

import logging
import os

from datetime import date

import requests

from mbu_rpa_core.exceptions import BusinessError, ProcessError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# API client helpers
# ---------------------------------------------------------------------------

def _get_api_credentials() -> tuple[str, str]:
    """Read befordring API endpoint and key from environment variables."""
    api_endpoint = os.getenv("API_ENDPOINT", "")
    api_key = os.getenv("API_KEY", "")
    if not api_endpoint or not api_key:
        raise ProcessError("API_ENDPOINT and API_KEY must be set in the environment / .env file.")
    return api_endpoint, api_key


def _fetch_lookup(api_endpoint: str, api_key: str, path: str) -> list[dict]:
    """GET a lookup list from the API and return the JSON body."""
    response = requests.get(
        f"{api_endpoint}{path}",
        headers={"X-API-Key": api_key},
        timeout=30,
    )
    if not response.ok:
        raise ProcessError(
            f"Lookup request failed [{path}]: {response.status_code} — {response.text}"
        )
    return response.json()


def _build_lookup_map(items: list[dict], name_key: str, id_key: str) -> dict[str, int]:
    """Build a case-insensitive name → id dict from a lookup list."""
    return {
        item[name_key].strip().lower(): item[id_key]
        for item in items
        if item.get(name_key)
    }


def _parse_date(value: str | None) -> str | None:
    """
    Return value unchanged if it parses as an ISO date (YYYY-MM-DD), else None.
    Prevents non-date strings from being sent to the API.
    """
    if not value:
        return None
    try:
        date.fromisoformat(str(value)[:10])
        return str(value)[:10]
    except ValueError:
        return None


# Hardcoded mapping from BefordringsData HjemmelForBevilling values to the
# corresponding hjemmel_tekst in the befordring database and the begrundelse
# field required by the API. These are the only values in the legacy PPR bevillings.
_HJEMMEL_MAPPING: dict[str, dict[str, str]] = {
    "§26, stk. 1, nr. 1 (afstand til og fra skole)": {
        "db_tekst":   "§ 26, stk. 1 afstand",
        "begrundelse": "Afstand",
    },
    "§26, stk. 1, nr. 2 (farlig skolevej)": {
        "db_tekst":   "§ 26, stk. 1 afstand",
        "begrundelse": "Farlig skolevej",
    },
    "§26, stk. 2 (befordring til og fra skole af elevmed sygdom/handicap)": {
        "db_tekst":   "§ 26, stk. 2 sygdom",
        "begrundelse": "Sygdom",
    },
}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def create_bevilling(
    ppr_case_id: str,
    person_ssn: str,
    bor_case_id: str,
    bevillinger: list[dict],
    adresse_id: str,
) -> None:
    """
    Ensures the student exists in the befordring app, then creates bevilling
    and koerselsraekke records via the API.

    Address creation is not handled here — adresse_id is expected to already
    exist in the Adresse table (populated by the nightly sync). This flow
    only ever references it.

    Flow per call:
      1. GET  /citizen/stamdata/{cpr}           — check existence
         POST /citizen/create_elev              — create if missing (cpr + adresse_id only)
      2. POST /bevilling/create_bevilling/{cpr} — once per bevilling
      3. POST /bevilling/create_koerselsraekke  — once per koerselsraekke

    Args:
        ppr_case_id:      Source PPR case ID (for logging).
        person_ssn:       Citizen SSN (CPR).
        person_full_name: Citizen full name resolved from GO contact lookup.
        bor_case_id:      Resolved BOR case ID, stored as esdh_noegle.
        bevillinger:      Grouped bevilling list from BefordringsData.
        adresse_id:       AdresseId resolved from LOIS at queue time.
    """
    api_endpoint, api_key = _get_api_credentials()
    headers = {"X-API-Key": api_key}

    # --- Fetch lookup tables once and build resolution maps ---
    tidspunkter = _fetch_lookup(api_endpoint, api_key, "/lookup/tidspunkter")
    koerselstyper = _fetch_lookup(api_endpoint, api_key, "/lookup/koerselstyper")
    hjemler = _fetch_lookup(api_endpoint, api_key, "/lookup/hjemler")
    sagsbehandlere = _fetch_lookup(api_endpoint, api_key, "/lookup/sagsbehandlere")
    skolematrikler = _fetch_lookup(api_endpoint, api_key, "/lookup/skolematrikel")

    # All lookup endpoints return {"id": ..., "label": ...}
    tidspunkt_map = _build_lookup_map(tidspunkter, name_key="label", id_key="id")
    koerselstype_map = _build_lookup_map(koerselstyper, name_key="label", id_key="id")
    sagsbehandler_map = _build_lookup_map(sagsbehandlere, name_key="label", id_key="id")

    # Plain name → id map for the DB hjemmel table
    hjemmel_map = _build_lookup_map(hjemler, name_key="label", id_key="id")

    # Skolematrikel map: skolekode (string) → matrikel_id
    # /lookup/skolematrikel returns {"id": matrikel_id, "label": naam, "skolekode": ...}
    # SkoleID from BefordringsData matches the skolekode column in the lookup
    skolematrikel_map: dict[str, int] = {
        str(item["skolekode"]).strip(): item["id"]
        for item in skolematrikler
        if item.get("skolekode") is not None
    }

    # --- Ensure student exists in the befordring app ---
    stamdata_response = requests.get(
        f"{api_endpoint}/citizen/stamdata/{person_ssn}",
        headers=headers,
        timeout=30,
    )
    if not stamdata_response.ok:
        raise ProcessError(
            f"Failed to check student existence for {person_ssn}: "
            f"{stamdata_response.status_code} — {stamdata_response.text}"
        )

    if stamdata_response.json() is None:
        logger.info("Student %s not found — creating Elev record.\n", person_ssn)

        elev_payload = {
            "cpr": person_ssn,
            "adresse_id": adresse_id,
        }

        create_elev_response = requests.post(
            f"{api_endpoint}/citizen/create_elev",
            json=elev_payload,
            headers=headers,
            timeout=30,
        )
        if not create_elev_response.ok:
            raise ProcessError(
                f"Failed to create Elev for {person_ssn}: "
                f"{create_elev_response.status_code} — {create_elev_response.text}"
            )
        logger.info("Created Elev for %s.\n", person_ssn)

    else:
        logger.info("Student %s already exists — skipping Elev creation.\n", person_ssn)

    # --- Fetch existing bevillinger for this student to avoid duplicates ---
    # If a bevilling with the same esdh_noegle (BOR case ID) already exists
    # for this student, the item has already been (partially) processed and
    # we skip it rather than creating a duplicate.
    existing_bev_response = requests.get(
        f"{api_endpoint}/bevilling/get_student_bevillinger/{person_ssn}",
        headers=headers,
        timeout=30,
    )
    if not existing_bev_response.ok:
        raise ProcessError(
            f"Failed to fetch existing bevillinger for {person_ssn}: "
            f"{existing_bev_response.status_code} — {existing_bev_response.text}"
        )

    existing_esdh_noegler = {
        b.get("esdh_noegle")
        for b in (existing_bev_response.json() or [])
        if b.get("esdh_noegle")
    }

    logger.info(
        "Creating %d bevilling(er) for SSN %s linked to BOR case %s\n",
        len(bevillinger),
        person_ssn,
        bor_case_id,
    )

    for i, bevilling in enumerate(bevillinger, start=1):
        koerselsraekker = bevilling.get("koerselsraekker", [])

        if bor_case_id in existing_esdh_noegler:
            logger.info(
                "  Bevilling %d/%d already exists for SSN %s (esdh_noegle: %s) — skipping.\n",
                i,
                len(bevillinger),
                person_ssn,
                bor_case_id,
            )
            continue

        # --- Resolve bevilling-level lookup IDs ---
        skole_id = str(bevilling.get("SkoleID") or "").strip()
        matrikel_id = skolematrikel_map.get(skole_id)

        raw_hjemmel = (bevilling.get("HjemmelForBevilling") or "").strip()
        hjemmel_entry = _HJEMMEL_MAPPING.get(raw_hjemmel)
        hjemmel_id = hjemmel_map.get(hjemmel_entry["db_tekst"].lower()) if hjemmel_entry else None
        begrundelse = hjemmel_entry["begrundelse"] if hjemmel_entry else None

        raw_sagsbehandler = (bevilling.get("Sagsbehandler") or "").strip().lower()
        sagsbehandler_id = sagsbehandler_map.get(raw_sagsbehandler)

        revurderingsdato = _parse_date(bevilling.get("Revurdering"))

        # Past revurderingsdato from the source data has no meaning on the
        # newly created bevilling — null it out so the system does not
        # immediately flag it for re-review.  Future dates are kept as-is.
        if revurderingsdato and date.fromisoformat(revurderingsdato) < date.today():
            revurderingsdato = None

        bevilling_payload = {
            "adresse_id": adresse_id,
            "matrikel_id": matrikel_id,
            "hjemmel_id": hjemmel_id,
            "sagsbehandler_id": sagsbehandler_id,
            "esdh_noegle": bor_case_id,
            "revurderingsdato": revurderingsdato,
            "begrundelse_fra_formular": begrundelse,
            "ansoegningstype": "Kørsel",
            "ansoegningsdato": _parse_date(bevilling.get("CreationDate")),
        }

        # Strip None values — API uses exclude_none=True server-side
        bevilling_payload = {k: v for k, v in bevilling_payload.items() if v is not None}

        logger.info(
            "  Bevilling %d/%d | skole: %s (id: %s) | hjemmel: %r → %s | %d koerselsraekke(r)\n",
            i,
            len(bevillinger),
            bevilling.get("SkoleNavnBefordring"),
            matrikel_id,
            bevilling.get("HjemmelForBevilling"),
            hjemmel_id,
            len(koerselsraekker),
        )

        # --- POST: create bevilling ---
        create_bev_response = requests.post(
            f"{api_endpoint}/bevilling/create_bevilling/{person_ssn}",
            json=bevilling_payload,
            headers=headers,
            timeout=30,
        )

        if not create_bev_response.ok:
            raise ProcessError(
                f"Failed to create bevilling {i}/{len(bevillinger)} "
                f"for PPR case {ppr_case_id}: "
                f"{create_bev_response.status_code} — {create_bev_response.text}"
            )

        new_bevilling_id = create_bev_response.json().get("bevilling_id")
        logger.info("  Created bevilling id: %s\n", new_bevilling_id)

        # --- POST: create each koerselsraekke ---
        for j, kr in enumerate(koerselsraekker, start=1):
            raw_tidspunkt = (kr.get("TidspunktForBevilling") or "").strip().lower()
            raw_koerselstype = (kr.get("BevillingAfKoerselstype") or "").strip().lower()

            tidspunkt_id = tidspunkt_map.get(raw_tidspunkt)
            befordringstype_id = koerselstype_map.get(raw_koerselstype)

            if not tidspunkt_id:
                raise BusinessError(
                    f"Unknown TidspunktForBevilling {kr.get('TidspunktForBevilling')!r} "
                    f"for PPR case {ppr_case_id} — check lookup table."
                )
            if not befordringstype_id:
                raise BusinessError(
                    f"Unknown BevillingAfKoerselstype {kr.get('BevillingAfKoerselstype')!r} "
                    f"for PPR case {ppr_case_id} — check lookup table."
                )

            koersel_payload = {
                "gyldig_fra": kr.get("BevillingFra"),
                "gyldig_til": kr.get("BevillingTil"),
                "tidspunkt_id": tidspunkt_id,
                "befordringstype_id": befordringstype_id,
                "bevilget_koereafstand_pr_vej": kr.get("BevilgetKoereAfstand") or None,
                "kommentar": kr.get("Kommentar") or None,
            }

            koersel_payload = {k: v for k, v in koersel_payload.items() if v is not None}

            logger.info(
                "    Koerselsraekke %d/%d | %s -> %s | type: %s | tidspunkt: %s\n",
                j,
                len(koerselsraekker),
                kr.get("BevillingFra"),
                kr.get("BevillingTil"),
                kr.get("BevillingAfKoerselstype"),
                kr.get("TidspunktForBevilling"),
            )

            create_kr_response = requests.post(
                f"{api_endpoint}/bevilling/create_koerselsraekke/{new_bevilling_id}",
                json=koersel_payload,
                headers=headers,
                timeout=30,
            )

            if not create_kr_response.ok:
                raise ProcessError(
                    f"Failed to create koerselsraekke {j}/{len(koerselsraekker)} "
                    f"on bevilling {new_bevilling_id} for PPR case {ppr_case_id}: "
                    f"{create_kr_response.status_code} — {create_kr_response.text}"
                )

            new_koersel_id = create_kr_response.json().get("koersel_id")
            logger.info("    Created koerselsraekke id: %s\n", new_koersel_id)
