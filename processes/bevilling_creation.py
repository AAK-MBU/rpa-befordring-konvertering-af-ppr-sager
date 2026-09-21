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

def get_api_credentials() -> tuple[str, str]:
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


def _normalise(value: str | None) -> str:
    """Reduce a lookup label to a form the two systems agree on.

    Casefolded with ALL whitespace removed, because BefordringsData and the
    befordring database do not space these consistently:

        BefordringsData   "Egenbefordring"
        Befordringstype   "Egen befordring"

    Verified against the seeded lookups — Befordringstype, Tidspunkt,
    Rutetype, KoerselstypeTillaeg, Hjemmel and Ugedag — that removing spaces
    collapses no two values onto each other, so nothing becomes ambiguous.

    This is the third place in the system to need it: view_Koerselsgodtgoerelse
    _Modtagere strips spaces before comparing, and so does labelIsEgenbefordring
    in the frontend. recalculateEgenbefordringRows silently never fired for
    months because it did not.
    """

    return "".join(str(value or "").split()).casefold()


def _build_lookup_map(items: list[dict], name_key: str, id_key: str) -> dict[str, int]:
    """Build a whitespace- and case-insensitive name → id dict."""
    return {
        _normalise(item[name_key]): item[id_key]
        for item in items
        if item.get(name_key)
    }


def _build_label_map(items: list[dict], name_key: str) -> dict[str, str]:
    """normalised key → the label as the befordring database spells it."""
    return {
        _normalise(item[name_key]): item[name_key]
        for item in items
        if item.get(name_key)
    }


def _resolve(
    lookup: dict[str, int],
    labels: dict[str, str],
    raw: str | None,
    what: str,
) -> int | None:
    """Look a value up, reporting when the two systems spell it differently.

    Fuzzy matching that says nothing hides the data problem it papers over.
    The comparison is source against the STORED label — not against the
    normalised key — so a value the two systems already agree on stays silent
    however many spaces it contains.
    """

    if not raw:
        return None

    key = _normalise(raw)
    found = lookup.get(key)

    if found is not None:
        stored = labels.get(key, "")

        if str(raw).strip().casefold() != stored.strip().casefold():
            logger.info(
                "  %s %r stored as %r — matched after normalising.",
                what,
                str(raw).strip(),
                stored,
            )

    return found


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


# Every converted bevilling is assigned to this caseworker.
#
# BefordringsData has a Sagsbehandler column, but the names in it are legacy
# PPR staff and do not line up with the Sagsbehandler table — which is not
# seeded reference data but real people, created and retired as staff change.
# Matching on those names would leave most converted bevillinger with no
# caseworker at all, and occasionally attach one to someone who has left.
#
# A single known owner is more useful: every converted bevilling is
# identifiable and reassignable in one go. Change this when the business
# decides who should own them.
_SAGSBEHANDLER_NAVN = "Sofie"


# BefordringsData has no rutetype — the new system does, and its own form
# requires one. The legacy TidspunktForBevilling implies it well enough:
# a morning-only bevilling runs one way, an afternoon-only one the other, and
# a bevilling covering both runs in both directions.
#
# Written the way the Tidspunkt table spells them and normalised below, rather
# than pre-normalised by hand. Hand-written keys broke once: "Morgen og
# eftermiddag" keyed as "morgen og eftermiddag" stopped matching the moment
# _normalise began removing interior spaces, and because the two single-word
# values kept working the gap was invisible — every bevilling covering both
# directions was created with no rutetype at all.
#
# Values must match Rutetype.rutetype_tekst — note "Skole til hjem", not
# "Fra skole til hjem".
_RUTETYPE_FROM_TIDSPUNKT_RAW: dict[str, str] = {
    "Morgen": "Hjem til skole",
    "Eftermiddag": "Skole til hjem",
    "Morgen og eftermiddag": "Mellem hjem og skole",
}


# Keyed exactly as every other lookup is, so the two cannot drift apart.
_RUTETYPE_FROM_TIDSPUNKT: dict[str, str] = {
    _normalise(tidspunkt): rutetype
    for tidspunkt, rutetype in _RUTETYPE_FROM_TIDSPUNKT_RAW.items()
}


def _split_koerselstype(
    raw: str | None,
    koerselstype_map: dict[str, int],
    tillaeg_map: dict[str, int],
) -> tuple[int | None, list[int]]:
    """Resolve a BevillingAfKoerselstype, peeling off any trailing tillæg.

    The source combines the two into one string where the new system keeps
    them apart:

        BefordringsData   "Rutekørsel fast forsæde"
        Befordringstype   "Rutekørsel"
        KoerselstypeTillaeg                "Fast forsæde"

    A direct match is tried first, so a plain kørselstype can never be
    mis-split. Only when that fails are known tillæg stripped from the end,
    longest first — "Fast forsæde" and "Fast sæde" both exist, and taking the
    shorter one first would leave "…for" stuck on the front of the type.

    The loop rather than a single strip means a value naming two tillæg
    resolves too, without anyone adding a case for it.

    Returns (befordringstype_id, tillaeg_ids). The id is None when nothing
    matched, which the caller turns into a BusinessError.
    """

    key = _normalise(raw)

    if not key:
        return None, []

    tillaeg_ids: list[int] = []

    # Longest first: see the docstring.
    by_length = sorted(tillaeg_map, key=len, reverse=True)

    while True:
        found = koerselstype_map.get(key)

        if found is not None:
            return found, tillaeg_ids

        for tillaeg_key in by_length:
            if key.endswith(tillaeg_key) and len(key) > len(tillaeg_key):
                key = key[: -len(tillaeg_key)]
                tillaeg_ids.append(tillaeg_map[tillaeg_key])
                break
        else:
            # Nothing left to peel and still no match.
            return None, []


def _has_koerselsraekker(api_endpoint: str, headers: dict, bevilling_id: int) -> bool:
    """Does this bevilling already have its kørselsrækker?

    A bevilling is created before them, so one with none is not finished — it
    is the debris of a run that stopped in between. Treating it as done is how
    a bevilling ends up stuck at Påbegyndt with nothing on it.
    """

    response = requests.get(
        f"{api_endpoint}/bevilling/get_bevilling_koerselsraekker/{bevilling_id}",
        headers=headers,
        timeout=30,
    )

    if not response.ok:
        raise ProcessError(
            f"Failed to read kørselsrækker for bevilling {bevilling_id}: "
            f"{response.status_code} — {response.text}"
        )

    return bool(response.json())


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def create_bevilling(
    ppr_case_id: str,
    person_ssn: str,
    bevillinger: list[dict],
) -> None:
    """
    Creates bevilling and koerselsraekke records via the API for a student
    who is already in Elev.

    Addresses are not created here. Each bevilling carries the adresse_id it
    was granted against, resolved at queue time against the Adresse table —
    which the nightly run keeps as a full copy of the municipality's register.
    This flow only ever references it.

    Flow per call:
      1. GET  /citizen/stamdata/{cpr}           — must exist; BusinessError if not
      2. POST /bevilling/create_bevilling/{cpr} — once per bevilling
      3. POST /bevilling/create_koerselsraekke  — once per koerselsraekke

    Args:
        ppr_case_id:      Source PPR case ID (for logging).
        person_ssn:       Citizen SSN (CPR).
        person_full_name: Citizen full name resolved from GO contact lookup.
        bevillinger:      Grouped bevilling list from BefordringsData. Each
                          entry carries its own adresse_id.
    """
    api_endpoint, api_key = get_api_credentials()
    headers = {"X-API-Key": api_key}

    # --- Fetch lookup tables once and build resolution maps ---
    tidspunkter = _fetch_lookup(api_endpoint, api_key, "/lookup/tidspunkter")
    koerselstyper = _fetch_lookup(api_endpoint, api_key, "/lookup/koerselstyper")
    hjemler = _fetch_lookup(api_endpoint, api_key, "/lookup/hjemler")
    sagsbehandlere = _fetch_lookup(api_endpoint, api_key, "/lookup/sagsbehandlere")
    skolematrikler = _fetch_lookup(api_endpoint, api_key, "/lookup/skolematrikel")
    dage = _fetch_lookup(api_endpoint, api_key, "/lookup/dage")
    rutetyper = _fetch_lookup(api_endpoint, api_key, "/lookup/rutetyper")
    koerselstype_tillaeg = _fetch_lookup(api_endpoint, api_key, "/lookup/koerselstype_tillaeg")

    # All lookup endpoints return {"id": ..., "label": ...}
    tidspunkt_map = _build_lookup_map(tidspunkter, name_key="label", id_key="id")
    koerselstype_map = _build_lookup_map(koerselstyper, name_key="label", id_key="id")

    # How the befordring database spells each one, for the mismatch report in
    # _resolve. BefordringsData says "Egenbefordring" where the lookup says
    # "Egen befordring", and that is worth seeing rather than silently fixing.
    tidspunkt_labels = _build_label_map(tidspunkter, name_key="label")
    koerselstype_labels = _build_label_map(koerselstyper, name_key="label")
    sagsbehandler_map = _build_lookup_map(sagsbehandlere, name_key="label", id_key="id")

    sagsbehandler_id = sagsbehandler_map.get(_normalise(_SAGSBEHANDLER_NAVN))

    if sagsbehandler_id is None:
        raise ProcessError(
            f"No sagsbehandler named {_SAGSBEHANDLER_NAVN!r} in the "
            "Sagsbehandler table. That table holds real staff and is not "
            "seeded, so it has to be populated before a conversion runs."
        )

    # Plain name → id map for the DB hjemmel table
    hjemmel_map = _build_lookup_map(hjemler, name_key="label", id_key="id")

    rutetype_map = _build_lookup_map(rutetyper, name_key="label", id_key="id")

    # "Rutekørsel fast forsæde" is one string in the source and two things
    # here — see _split_koerselstype.
    tillaeg_map = _build_lookup_map(koerselstype_tillaeg, name_key="label", id_key="id")
    tillaeg_labels = _build_label_map(koerselstype_tillaeg, name_key="label")

    # Fail at startup, not per row: every tidspunkt that survives the check in
    # the kørselsrække loop is one of these three, so a target that no longer
    # exists in the Rutetype table would otherwise silently leave rutetype_id
    # unset on every single converted række.
    missing_rutetyper = [
        navn
        for navn in _RUTETYPE_FROM_TIDSPUNKT.values()
        if _normalise(navn) not in rutetype_map
    ]

    if missing_rutetyper:
        raise ProcessError(
            "Rutetype(r) named in _RUTETYPE_FROM_TIDSPUNKT do not exist in "
            f"the Rutetype lookup table: {', '.join(missing_rutetyper)}. "
            "Check for a renamed value."
        )

    # And the other direction. Without this, a key that stops matching leaves
    # rutetype_id quietly unset on every række using that tidspunkt, while the
    # others carry on working and hide it — which is exactly what happened to
    # "Morgen og eftermiddag".
    unmapped_tidspunkter = [
        item["label"]
        for item in tidspunkter
        if item.get("label") and _normalise(item["label"]) not in _RUTETYPE_FROM_TIDSPUNKT
    ]

    if unmapped_tidspunkter:
        raise ProcessError(
            "Tidspunkt(er) in the lookup table have no entry in "
            f"_RUTETYPE_FROM_TIDSPUNKT: {', '.join(unmapped_tidspunkter)}. "
            "Every tidspunkt must imply a rutetype, or converted "
            "kørselsrækker are left without one."
        )

    # "Alle" — the weekday option meaning every school day.
    #
    # BefordringsData records no weekdays at all, but the application's own
    # kørselsrække form requires them: a converted række left without any
    # cannot be saved when a caseworker next edits it. "Alle" is the closest
    # honest default for a standing arrangement, and DagePicker treats it as
    # mutually exclusive with the individual days, so a caseworker narrowing it
    # later replaces it rather than adding to it.
    dag_map = _build_lookup_map(dage, name_key="label", id_key="id")
    alle_dage_id = dag_map.get(_normalise("Alle"))

    if alle_dage_id is None:
        raise ProcessError(
            "No 'Alle' entry in /lookup/dage — cannot set weekdays on converted "
            "kørselsrækker. Check the Ugedag lookup table."
        )

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
        # Every currently enrolled student is already in Elev: the nightly run
        # (rpa-befordring-nightly-runs) upserts the whole student dump from
        # Elev_STG before this conversion ever runs. A miss therefore means the
        # nightly load does not know this person — they have most likely left
        # the municipality or finished school since the legacy bevilling was
        # written.
        #
        # This used to POST /citizen/create_elev with just {cpr, adresse_id}.
        # That produced a row with no name, no skolekode and no klassetrin,
        # which nothing would ever fill in — the nightly upsert only touches
        # CPRs present in Elev_STG, and this one is not. It would also stay
        # outside the school derivation for good, because
        # usp_sync_elev_matrikel_from_bevilling requires a non-zero skolekode
        # before it will take a matrikel from the bevilling. No school means no
        # walking distance either.
        #
        # BusinessError rather than ProcessError: the item goes to
        # pending_user, so the case is parked for a human rather than failed
        # and retried. Nothing here can resolve it.
        raise BusinessError(
            f"Student {person_ssn} (PPR case {ppr_case_id}) is not in Elev. "
            "The nightly student load does not know this CPR — they may have "
            "left the municipality or finished school. Needs manual review "
            "before the bevilling can be converted."
        )

    logger.info("Student %s exists in Elev.\n", person_ssn)

    # --- Fetch existing bevillinger for this student to avoid duplicates ---
    # Keyed on (esdh_noegle, foerste_koersel_dato), not esdh_noegle alone.
    #
    # Every bevilling converted from one PPR case carries the same
    # esdh_noegle — the case id — so that alone cannot tell them apart. The
    # old check asked "does this case have any bevilling?", which meant a run
    # that died after creating the first of three would, on retry, skip all
    # three: the two that were never created included.
    #
    # foerste_koersel_dato is the earliest BevillingFra in the bucket, so the
    # pair identifies a single converted bevilling and a retry resumes exactly
    # where it stopped.
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

    def _identity(esdh_noegle, foerste_koersel_dato) -> tuple[str, str] | None:
        """The pair identifying one converted bevilling, or None if unusable."""

        if not esdh_noegle or not foerste_koersel_dato:
            return None

        return (str(esdh_noegle), str(foerste_koersel_dato)[:10])

    # identity -> bevilling_id, not just a set of identities: a bevilling that
    # exists is not necessarily finished, and completing it needs its id.
    existing_bevillinger: dict[tuple[str, str], int] = {}

    for existing in existing_bev_response.json() or []:
        identity = _identity(
            existing.get("esdh_noegle"), existing.get("foerste_koersel_dato")
        )

        if identity and existing.get("bevilling_id") is not None:
            existing_bevillinger[identity] = existing["bevilling_id"]

    logger.info(
        "Creating %d bevilling(er) for SSN %s linked to PPR case %s\n",
        len(bevillinger),
        person_ssn,
        ppr_case_id,
    )

    for i, bevilling in enumerate(bevillinger, start=1):
        koerselsraekker = bevilling.get("koerselsraekker", [])
        foerste_koersel_dato = bevilling.get("foerste_koersel_dato")

        identity = _identity(ppr_case_id, foerste_koersel_dato)
        existing_id = existing_bevillinger.get(identity) if identity else None

        # A bevilling is created before its kørselsrækker, so a run that died
        # between the two leaves one with none. Skipping on "the bevilling
        # exists" would strand it that way for good — which is exactly what
        # happened: a bevilling sitting at Påbegyndt with no kørselsrækker,
        # skipped on every retry.
        #
        # So existence is not the question. The question is whether it already
        # has its kørselsrækker.
        if existing_id is not None:
            if _has_koerselsraekker(api_endpoint, headers, existing_id):
                logger.info(
                    "  Bevilling %d/%d already complete for SSN %s "
                    "(id: %s, esdh_noegle: %s, foerste_koersel_dato: %s) — skipping.\n",
                    i,
                    len(bevillinger),
                    person_ssn,
                    existing_id,
                    ppr_case_id,
                    foerste_koersel_dato,
                )
                continue

            logger.warning(
                "  Bevilling %d/%d exists for SSN %s (id: %s) but has no "
                "kørselsrækker — a previous run stopped part-way. Completing "
                "it rather than creating a duplicate.\n",
                i,
                len(bevillinger),
                person_ssn,
                existing_id,
            )

        # --- Resolve bevilling-level lookup IDs ---
        skole_id = str(bevilling.get("SkoleID") or "").strip()
        matrikel_id = skolematrikel_map.get(skole_id)

        raw_hjemmel = (bevilling.get("HjemmelForBevilling") or "").strip()
        hjemmel_entry = _HJEMMEL_MAPPING.get(raw_hjemmel)
        hjemmel_id = hjemmel_map.get(_normalise(hjemmel_entry["db_tekst"])) if hjemmel_entry else None
        begrundelse = hjemmel_entry["begrundelse"] if hjemmel_entry else None

        revurderingsdato = _parse_date(bevilling.get("Revurdering"))

        # Past revurderingsdato from the source data has no meaning on the
        # newly created bevilling — null it out so the system does not
        # immediately flag it for re-review.  Future dates are kept as-is.
        if revurderingsdato and date.fromisoformat(revurderingsdato) < date.today():
            revurderingsdato = None

        # --- Resolve every kørselsrække BEFORE anything is written ---
        #
        # These used to be resolved inside the POST loop, i.e. after the
        # bevilling already existed. One unmappable value then raised
        # BusinessError with a bevilling already in the database — left at
        # Påbegyndt with no kørselsrækker, and skipped as "already exists" on
        # every retry. That is exactly how "Egenbefordring" (the source's
        # spelling of "Egen befordring") stranded a case.
        #
        # Validating first means a bad value fails the case without writing
        # anything, which is the only safe order for a one-shot migration.
        prepared: list[tuple[dict, str | None, dict]] = []

        for kr in koerselsraekker:
            raw_tidspunkt = _normalise(kr.get("TidspunktForBevilling"))

            tidspunkt_id = _resolve(
                tidspunkt_map,
                tidspunkt_labels,
                kr.get("TidspunktForBevilling"),
                "Tidspunkt",
            )
            raw_koerselstype = kr.get("BevillingAfKoerselstype")

            befordringstype_id = _resolve(
                koerselstype_map,
                koerselstype_labels,
                raw_koerselstype,
                "Kørselstype",
            )

            tillaeg_ids: list[int] = []

            if befordringstype_id is None:
                # Not a plain kørselstype — try it as a kørselstype plus one
                # or more tillæg run together.
                befordringstype_id, tillaeg_ids = _split_koerselstype(
                    raw_koerselstype, koerselstype_map, tillaeg_map
                )

                if befordringstype_id is not None:
                    logger.info(
                        "  Kørselstype %r split into a type plus tillæg: %s",
                        str(raw_koerselstype).strip(),
                        ", ".join(
                            tillaeg_labels.get(k, str(v))
                            for k, v in tillaeg_map.items()
                            if v in tillaeg_ids
                        ),
                    )

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

            # Derived from the tidspunkt — see _RUTETYPE_FROM_TIDSPUNKT. Safe
            # unguarded: the check above rejects anything outside the three
            # known values, and the startup check proved each maps to a real
            # rutetype.
            rutetype_navn = _RUTETYPE_FROM_TIDSPUNKT.get(raw_tidspunkt)
            rutetype_id = rutetype_map.get(_normalise(rutetype_navn)) if rutetype_navn else None

            koersel_payload = {
                "gyldig_fra": kr.get("BevillingFra"),
                "gyldig_til": kr.get("BevillingTil"),
                "tidspunkt_id": tidspunkt_id,
                "befordringstype_id": befordringstype_id,
                "rutetype_id": rutetype_id,
                "bevilget_koereafstand_pr_vej": kr.get("BevilgetKoereAfstand") or None,
                "kommentar": kr.get("Kommentar") or None,
                "dag_ids": [alle_dage_id],
                "tillaeg_ids": tillaeg_ids,
            }

            koersel_payload = {k: v for k, v in koersel_payload.items() if v is not None}

            prepared.append((koersel_payload, rutetype_navn, kr))

        bevilling_payload = {
            # Per bevilling, not per case: a student who moved has older
            # bevillinger at the previous address. process_item has already
            # rejected the case if any of these is missing.
            "adresse_id": bevilling.get("adresse_id"),
            "matrikel_id": matrikel_id,
            "hjemmel_id": hjemmel_id,
            "sagsbehandler_id": sagsbehandler_id,
            # The PPR case id. There is no separate ESDH case to resolve —
            # the borgersag flow this bot once had was scrapped, so the source
            # case id is the reference that goes on the bevilling.
            "esdh_noegle": ppr_case_id,
            "revurderingsdato": revurderingsdato,
            "begrundelse_fra_formular": begrundelse,
            # "Kørsel" until migration 009 in the befordring repo renamed it
            # and split it into "Fast kørsel" / "Midlertidig kørsel". The API
            # field is a free string, so the old value wrote fine and simply
            # rendered blank in the dropdown. Legacy PPR bevillinger are all
            # standing arrangements, so "Fast kørsel" is the right half.
            "ansoegningstype": "Fast kørsel",
            "ansoegningsdato": _parse_date(bevilling.get("CreationDate")),
            # Newest Modified across the bucket's source rows — computed in
            # queue_handler, where the rows are still available.
            "sagsbehandlingsdato": _parse_date(bevilling.get("sagsbehandlingsdato")),
            # Also the de-duplication key — see above.
            "foerste_koersel_dato": foerste_koersel_dato,
        }

        # Strip None values — API uses exclude_none=True server-side
        bevilling_payload = {k: v for k, v in bevilling_payload.items() if v is not None}

        logger.info(
            "  Bevilling %d/%d [%s] | skole: %s (id: %s) | hjemmel: %r → %s | "
            "%d koerselsraekke(r)\n",
            i,
            len(bevillinger),
            bevilling.get("bucket", "?"),
            bevilling.get("SkoleNavnBefordring"),
            matrikel_id,
            bevilling.get("HjemmelForBevilling"),
            hjemmel_id,
            len(koerselsraekker),
        )

        # --- POST: create bevilling, unless completing an existing one ---
        if existing_id is not None:
            new_bevilling_id = existing_id
        else:
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

            if identity:
                existing_bevillinger[identity] = new_bevilling_id

        # --- POST: create each koerselsraekke ---
        #
        # The payloads were resolved and validated before the bevilling was
        # created, so nothing here can fail on an unmappable lookup value.
        for j, (koersel_payload, rutetype_navn, kr) in enumerate(prepared, start=1):
            logger.info(
                "    Koerselsraekke %d/%d | %s -> %s | type: %s | "
                "tidspunkt: %s -> rutetype: %s\n",
                j,
                len(prepared),
                kr.get("BevillingFra"),
                kr.get("BevillingTil"),
                kr.get("BevillingAfKoerselstype"),
                kr.get("TidspunktForBevilling"),
                rutetype_navn,
            )

            create_kr_response = requests.post(
                f"{api_endpoint}/bevilling/create_koerselsraekke/{new_bevilling_id}",
                json=koersel_payload,
                headers=headers,
                timeout=30,
            )

            if not create_kr_response.ok:
                raise ProcessError(
                    f"Failed to create koerselsraekke {j}/{len(prepared)} "
                    f"on bevilling {new_bevilling_id} for PPR case {ppr_case_id}: "
                    f"{create_kr_response.status_code} — {create_kr_response.text}"
                )

            new_koersel_id = create_kr_response.json().get("koersel_id")
            logger.info("    Created koerselsraekke id: %s\n", new_koersel_id)
