"""
Bevilling flow: create bevilling and koerselsraekke records in the
befordring application via its REST API.
"""

import logging
import os
import re
import unicodedata

from datetime import date

import requests

from mbu_rpa_core.exceptions import BusinessError, ProcessError

from helpers import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# API client helpers
# ---------------------------------------------------------------------------

def get_api_credentials() -> tuple[str, str]:
    """Read befordring API endpoint and key from environment variables."""
    api_endpoint = os.getenv("BEFORDRING_API_ENDPOINT", "")
    api_key = os.getenv("BEFORDRING_API_KEY", "")
    if not api_endpoint or not api_key:
        raise ProcessError("BEFORDRING_API_ENDPOINT and BEFORDRING_API_KEY must be set in the environment / .env file.")
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


_MATRIKEL_STED = re.compile(r"\(([^)]+)\)")
_MATRIKEL_SKOLE = re.compile(r"^([^(]+)")


def _uden_accent(value: str | None) -> str:
    """_normalise, with accents folded away as well.

    The source writes the site without its accent:

        Skolematrikel     Tranbjergskolen (Grønløkke Allé)
        SkolensAdresse    Grønløkke Alle

    "allé" and "alle" are one street written two ways, and comparing them as
    they stand leaves a bevilling unplaceable for the sake of one character.

    æ/ø/å are flattened to ae/oe/aa for the same reason — the source writes
    "Groenloekke" as readily as "Grønløkke" — matching how _fold treats klub
    markers and addresses elsewhere.

    NFD splits a letter from its accent and the combining marks are dropped,
    which takes care of é and of the å that ae/oe/aa flattening leaves.

    Verified against all ten seeded site names: no pair of sites sharing a
    skolekode collapses onto the other under this fold, so nothing becomes
    ambiguous that was not already. Both sides are folded identically in any
    case, so a fold can only ever make two spellings of one name agree.
    """

    foldet = _normalise(value)

    for saerlig, almindelig in (("æ", "ae"), ("ø", "oe"), ("å", "aa")):
        foldet = foldet.replace(saerlig, almindelig)

    nedbrudt = unicodedata.normalize("NFD", foldet)

    return "".join(c for c in nedbrudt if not unicodedata.combining(c))


def _extend_kommentar(kommentar: str | None, note: str | None) -> str | None:
    """Append a note to a comment, keeping whichever of the two exists."""

    eksisterende = str(kommentar or "").strip()

    if not note:
        return eksisterende or None

    return f"{eksisterende}\n\n{note}" if eksisterende else note


def _navngiver(tekst: str, navn: str) -> bool:
    """Does `tekst` name `navn` — at a word boundary, not mid-word?"""

    start = tekst.find(navn)

    while start != -1:
        if start == 0 or not tekst[start - 1].isalpha():
            return True

        start = tekst.find(navn, start + 1)

    return False


def _matrikler_andetsteds(
    skole_par: list,
    alle_matrikler: list[dict] | None,
    skolekode: object,
) -> list[dict]:
    """Matrikler under a DIFFERENT skolekode that the rows' own columns name.

    A stale SkoleID reads exactly like an unplaceable one: the code's own
    sites are searched, none of them matches, and the message ends "ingen af
    dem peger på en afdeling" — which sounds like the address was at fault.
    Usually it was not. A student changing school has the new school written
    on the row while SkoleID still holds the old school's code:

        SkoleID               751020                              (Kaløvigskolen)
        SkoleNavnBefordring   Langagerskolen
        SkolensAdresse        Kolt Østervej 45, 8361 Hasselager   (751090)

    Searching the whole table for what the row actually names turns that into
    something a caseworker can act on: the columns are right, the code is
    wrong, and BefordringsData is what needs correcting.

    For the message only — nothing is resolved from it. The matrikel still
    has to come from SkoleID, because trusting free text over the code is how
    a live bevilling ends up at the wrong school.

    Matched on the label, which is all /lookup/skolematrikel returns: the
    school name before the parenthesis against SkoleNavnBefordring, and the
    site inside it against SkolensAdresse — so "Langagerskolen" alone finds
    both Langagerskolen sites and "Kolt Østervej 45" narrows them to one.

    A row that names the school but not the site still counts. Both sites
    then come back, which is enough: they share a skolekode, and the
    skolekode is the field being questioned.

    The name has to sit on a word boundary, not merely occur somewhere in the
    string. Folding removes the spaces, and "Ellevangskolen" is a substring of
    "Møllevangskolen" — two real Aarhus schools under two skolekoder — so a
    bare containment test names Ellevangskolen on every Møllevangskolen row.
    Start of string, or a non-letter in front, is the whole guard.
    """

    if not alle_matrikler:
        return []

    kode = str(skolekode or "").strip()
    praecise: dict[int, dict] = {}
    navn_kun: dict[int, dict] = {}

    for par in skole_par or []:
        adresse, navn = (list(par) + ["", ""])[:2]
        navn_foldet = _uden_accent(navn)

        if not navn_foldet:
            continue

        haystack = _uden_accent(adresse) + "\x00" + navn_foldet

        for k in alle_matrikler:
            if str(k.get("skolekode") or "").strip() == kode:
                continue

            label = str(k.get("label") or "")
            skole = _MATRIKEL_SKOLE.match(label)
            skole_foldet = _uden_accent(skole.group(1) if skole else label)

            if not skole_foldet or not _navngiver(navn_foldet, skole_foldet):
                continue

            sted = _MATRIKEL_STED.search(label)

            if sted and _uden_accent(sted.group(1)) not in haystack:
                navn_kun[k["id"]] = k
                continue

            praecise[k["id"]] = k

    return list((praecise or navn_kun).values())


def _forkert_skoleid(andre: list[dict], skolekode: object) -> str:
    """One sentence naming the skolekode the rows actually describe."""

    navne = ", ".join(sorted(str(k.get("label")) for k in andre))
    koder = " / ".join(sorted({str(k.get("skolekode")) for k in andre}))

    return (
        f"SkoleID ser forældet ud: rækkernes skolekolonner peger på {navne} "
        f"— skolekode {koder}, ikke {skolekode}. Ret SkoleID i "
        "BefordringsData."
    )


def _manuel_matrikel(
    ppr_case_id: str | None,
    skolekode: object,
    alle_matrikler: list[dict] | None,
) -> tuple[int, str] | None:
    """A hand-made decision from config.MATRIKEL_OVERRIDES, or None.

    Some cases cannot be placed from the rows at all. A case with ONLY klub
    kørsel — skole -> klub -> hjem, no home-to-school leg — has both school
    columns describing the klub on every row, so a skolekode split across two
    sites has nothing to choose on. The information is outside the data; a
    caseworker has it.

    Keyed on the PPR case ID, optionally narrowed by skolekode so a case with
    two bevillinger at two schools is not forced onto one of them.

    Named by LABEL, not by matrikel_id: ids are per-environment identity
    values and a number in a config file cannot be reviewed, while
    "Stensagerskolen (Stensagervej)" can. A label that does not exist raises
    ProcessError — a typo is an operator error, not a case a caseworker can
    resolve, and converting to a silently wrong school is the one outcome
    worth failing to avoid.
    """

    valg = (config.MATRIKEL_OVERRIDES or {}).get(str(ppr_case_id or "").strip())

    if not valg:
        return None

    kun_skolekode = str(valg.get("skolekode") or "").strip()

    if kun_skolekode and kun_skolekode != str(skolekode or "").strip():
        return None

    label = str(valg.get("matrikel") or "").strip()
    valgt = next(
        (k for k in (alle_matrikler or []) if str(k.get("label") or "") == label),
        None,
    )

    if valgt is None:
        raise ProcessError(
            f"MATRIKEL_OVERRIDES for PPR-sag {ppr_case_id} peger på "
            f"skolematrikel {label!r}, som ikke findes i /lookup/skolematrikel. "
            "Ret labelen i helpers/config.py — den skal staves nøjagtigt som "
            "i opslaget."
        )

    note = _konverterings_note(
        "skole sat manuelt",
        "Skolen kunne ikke udledes af rækkerne, så den er sat manuelt ved "
        "konverteringen.",
        str(valg.get("begrundelse") or "").strip()
        or "Ingen begrundelse angivet i konverteringens opsætning.",
        f"Valgt: {valgt.get('label')}",
        "Ret bevillingen, hvis den skal være anderledes.",
    )

    return valgt["id"], note


def _vaelg_matrikel(
    kandidater: list[dict],
    skole_par: list,
    bucket: str | None = None,
    alle_matrikler: list[dict] | None = None,
    ppr_case_id: str | None = None,
) -> tuple[int | None, str | None]:
    """Which matrikel a bevilling belongs to, when a skolekode has several.

    Some schools run on two sites under one skolekode:

        Stensagerskolen (Janesvej)        751903
        Stensagerskolen (Stensagervej)    751903

    The lookup label carries the site in parentheses, and BefordringsData
    carries SkolensAdresse — "Janesvej 2", "Stensagervej 11" — so the street
    in the address is what tells them apart. Matched on the normalised forms,
    so spacing and case do not matter and "Bøgeskov Høvej" finds "Bøgeskov
    Høvej 10". Accents are folded too, because the source drops them:
    "Grønløkke Alle" has to find "Tranbjergskolen (Grønløkke Allé)".
    SkoleNavnBefordring is tried as well, because the source sometimes names
    the site there instead: "Stensagerskolen (afd. Stensagervej)".

    EVERY row's pair is tried, not just the bevilling's first non-None.
    Those two columns are bevilling-level, so where the first row is a klub
    row they name the klub — "Nygårdsvej 5, 8270 Højbjerg" — and the site is
    unfindable even though a sibling row states it plainly.

    The rows must agree. One matrikel across all of them is the answer; two
    different ones is a real disagreement — some rows to Janesvej, some to
    Stensagervej — and that is a case for a human, not a coin toss.

    On a bucket of "past" an unsettled site is GUESSED rather than raised.
    That bevilling has expired: it is not routing anyone, its walking
    distance is not being recalculated, and the only cost of the wrong site
    is a field a caseworker may correct. Against that, raising loses the
    whole case — the student's active bevilling included. The guess is the
    first site by name, so it is stable across runs, and the kørselsrækker
    say plainly that it was a guess and what the source actually held.

    Anywhere else — current, future, ukendt — it still raises. Those are live
    bevillinger, and a wrong school there drives the walking distance, the
    skolekode comparison and the school derivation on Elev.

    Where nothing matches, the whole table is searched for what the rows DO
    name, so a stale SkoleID — the new school written on the row, the old
    school's code still in the column — is stated as such instead of reading
    like a bad address. It only sharpens the message; the site is never
    resolved from it. See _matrikler_andetsteds.

    A hand-made decision in config.MATRIKEL_OVERRIDES wins over all of it,
    including over an unknown skolekode. See _manuel_matrikel.

    Returns (matrikel_id, note). The note is None unless a guess or a manual
    decision was made. An unknown skolekode yields (None, None), as before.
    """

    manuel = _manuel_matrikel(
        ppr_case_id,
        kandidater[0].get("skolekode") if kandidater else None,
        alle_matrikler,
    )

    if manuel:
        return manuel

    if not kandidater:
        return None, None

    if len(kandidater) == 1:
        return kandidater[0]["id"], None

    fundet: dict[int, str] = {}

    for par in skole_par or []:
        adresse, navn = (list(par) + ["", ""])[:2]
        haystack = _uden_accent(adresse) + "\x00" + _uden_accent(navn)

        for k in kandidater:
            sted = _MATRIKEL_STED.search(str(k.get("label") or ""))

            if sted and _uden_accent(sted.group(1)) in haystack:
                fundet[k["id"]] = str(k.get("label"))

    if len(fundet) == 1:
        return next(iter(fundet)), None

    andre = _matrikler_andetsteds(
        skole_par, alle_matrikler, kandidater[0].get("skolekode")
    )
    hint = _forkert_skoleid(andre, kandidater[0].get("skolekode")) if andre else ""

    proevet = "; ".join(
        f"{(list(p) + ['', ''])[0]!r} / {(list(p) + ['', ''])[1]!r}"
        for p in skole_par or []
    ) or "(ingen skolekolonner på rækkerne)"

    # An expired bevilling is guessed rather than lost. See the docstring.
    if str(bucket or "").strip().casefold() == "past":
        valgt = min(kandidater, key=lambda k: str(k.get("label") or ""))

        note = _konverterings_note(
            "skoleafdeling gættet",
            f"Skolekode {kandidater[0].get('skolekode')} dækker flere "
            f"afdelinger: {', '.join(str(k.get('label')) for k in kandidater)}.",
            f"Rækkernes SkolensAdresse / SkoleNavnBefordring: {proevet}",
            hint or "Ingen af dem peger entydigt på en afdeling.",
            f"Valgt: {valgt.get('label')}",
            "Bevillingen er udløbet, så afdelingen har ingen praktisk "
            "betydning — men ret den, hvis den skal være rigtig.",
        )

        return valgt["id"], note

    raise BusinessError(
        f"Skolekode {kandidater[0].get('skolekode')} har "
        f"{len(kandidater)} matrikler "
        f"({', '.join(str(k.get('label')) for k in kandidater)}). "
        f"Rækkernes SkolensAdresse / SkoleNavnBefordring: {proevet}. "
        + (
            f"De peger på flere forskellige afdelinger ({', '.join(fundet.values())}) "
            "— rækkerne er uenige."
            if fundet
            else hint or "Ingen af dem peger på en afdeling."
        )
        + (
            " Kan ikke afgøre hvilken afdeling bevillingen hører til — "
            "kræver manuel opfølgning."
            if not hint
            else ""
        )
    )


# Every comment this conversion writes begins with this exact string, and
# nothing else in the application writes it. It is what makes the converted
# rows findable afterwards:
#
#     SELECT DISTINCT b.bevilling_id
#     FROM   befordring.Koersel k
#     JOIN   befordring.Bevilling b ON b.bevilling_id = k.bevilling_id
#     WHERE  k.kommentar LIKE '%KONVERTERING-PPR%';
#
# No square brackets, percent signs or underscores on purpose: all three are
# metacharacters in T-SQL LIKE, and a marker containing them would silently
# match far more than intended.
_KONVERTERING_MARKOER = "KONVERTERING-PPR"


def _konverterings_note(emne: str, *linjer: str) -> str:
    """One comment from the conversion, marked so it can be found again.

    Every note goes through here, so the marker cannot be forgotten on a new
    one, and the second field names the KIND of note — a caseworker scanning
    the list can tell a punctuation match from an assumed flat without
    reading the body.
    """

    return "\n".join([f"{_KONVERTERING_MARKOER} | {emne}", *linjer])


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


# The caseworker comes from BefordringsData's Author column, which holds the
# person who wrote the row: a full name followed by something in brackets —
# "Anne Sørensen (BU Befordring)". The bracketed part is an organisational
# label, not part of the name, and is dropped.
#
# Every converted bevilling used to be assigned to one hardcoded owner
# instead, on the grounds that the legacy names would not line up with the
# Sagsbehandler table. Author does line up, and it says who actually handled
# the case — which is worth more than a single reassignable owner.
_FORFATTER_PARENTES = re.compile(r"\s*[(\[][^)\]]*[)\]]\s*")


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

def _navn_fold(value: str | None) -> str:
    """Fold a person's name for comparison, KEEPING the spaces.

    _uden_accent cannot be used here: it removes all whitespace, which is
    right for a lookup label but destroys the word boundaries this match
    depends on — "Sofie Hansen" would collapse to "sofiehansen", and the
    label "Sofie" would no longer end on a boundary.

    Case folded, æ/ø/å flattened to ae/oe/aa, remaining accents stripped, and
    runs of whitespace collapsed to one space.
    """

    foldet = str(value or "").casefold()

    for saerlig, almindelig in (("æ", "ae"), ("ø", "oe"), ("å", "aa")):
        foldet = foldet.replace(saerlig, almindelig)

    nedbrudt = unicodedata.normalize("NFD", foldet)
    uden_accent = "".join(c for c in nedbrudt if not unicodedata.combining(c))

    return " ".join(uden_accent.split())


def _forfatter_navn(author: str | None) -> str:
    """The person's name from an Author value, without the bracketed part."""

    return " ".join(_FORFATTER_PARENTES.sub(" ", str(author or "")).split())


def _vaelg_sagsbehandler(
    author: str | None,
    sagsbehandlere: list[dict],
    ppr_case_id: str,
) -> int:
    """The Sagsbehandler id for a row's Author. Raises when it cannot be told.

    Matched on the folded name — case, spacing and accents ignored, and æ/ø/å
    flattened — because the two systems do not spell staff names identically
    and a caseworker is not going to be re-typed to make them agree.

    Three passes, narrowest first:

      1. the whole name equals the label
      2. the label is one or more whole words at the START of the name —
         "Sofie" matches "Sofie Hansen", because the table has historically
         held first names while Author holds the full one
      3. the label appears as whole words anywhere in the name

    A pass that finds EXACTLY ONE label wins. Two labels matching equally well
    is not a tie to break: "Anne" and "Anne Marie" are different people, and
    guessing between them puts a case on the wrong caseworker's list.

    BusinessError rather than a fallback owner. The case goes to pending_user
    with the name quoted, so it is fixed by adding the person to the
    Sagsbehandler table or correcting the row — both of which are right, where
    silently parking it on a stand-in is not.
    """

    navn = _navn_fold(_forfatter_navn(author))

    if not navn:
        raise BusinessError(
            f"PPR case {ppr_case_id}: BefordringsData has no Author on the "
            "row, so the bevilling has no caseworker to be assigned to. "
            "Needs manual follow-up."
        )

    kandidater = [
        (s, _navn_fold(s.get("label")))
        for s in sagsbehandlere
        if str(s.get("label") or "").strip()
    ]

    def _hele_ord(hay: str, naal: str, kun_start: bool) -> bool:
        start = hay.find(naal)

        while start != -1:
            foer_ok = start == 0
            efter = start + len(naal)
            efter_ok = efter == len(hay) or not hay[efter].isalpha()

            if efter_ok and (foer_ok or (not kun_start and not hay[start - 1].isalpha())):
                return True

            start = hay.find(naal, start + 1)

        return False

    for test in (
        lambda label: label == navn,
        lambda label: _hele_ord(navn, label, kun_start=True),
        lambda label: _hele_ord(navn, label, kun_start=False),
    ):
        traf = [s for s, label in kandidater if label and test(label)]

        if len(traf) == 1:
            return traf[0]["id"]

        if len(traf) > 1:
            raise BusinessError(
                f"PPR case {ppr_case_id}: Author "
                f"{_forfatter_navn(author)!r} matches several sagsbehandlere "
                f"({', '.join(sorted(str(s.get('label')) for s in traf))}). "
                "Cannot tell which one, and guessing would put the case on "
                "the wrong caseworker's list. Needs manual follow-up."
            )

    raise BusinessError(
        f"PPR case {ppr_case_id}: Author {_forfatter_navn(author)!r} is not "
        "in the Sagsbehandler table, so the bevilling has no caseworker. "
        "Add the person to that table, or correct the row. Needs manual "
        "follow-up."
    )


def _adresse_findes(api_endpoint: str, headers: dict, adresse_id: str) -> bool:
    """Is this adresse_id already a row in Adresse? False on any doubt."""

    try:
        response = requests.get(
            f"{api_endpoint}/adresse/{adresse_id}",
            headers=headers,
            timeout=30,
        )
    except requests.RequestException:
        return False

    return bool(response.ok and response.json())


def _cpr_cifre(cpr) -> str:
    """The ten digits of a CPR. Elev.cpr is CHAR(10), the source is not."""

    return "".join(c for c in str(cpr or "") if c.isdigit())


def _skolekode_fra_rows(bevillinger: list[dict]) -> int:
    """The first SkoleID that reads as a number, else 0."""

    for bevilling in bevillinger:
        raa = str(bevilling.get("SkoleID") or "").strip()

        if raa.isdigit():
            return int(raa)

    return 0


def _adresse_id_kilde_tekst(kilde: str) -> str:
    """The note's wording for where the address came from."""

    return (
        "adressen er folkeregisterets"
        if kilde == "LOIS"
        else "adressen er taget fra bevillingen, da folkeregisterets adresse "
        "ikke findes i adressetabellen"
    )


def _opret_elev_fra_lois(
    api_endpoint: str,
    headers: dict,
    person_ssn: str,
    ppr_case_id: str,
    lois_person: dict | None,
    bevillinger: list[dict],
) -> tuple[str, str]:
    """Create the student in Elev from LOIS. Returns (adresse_id, note).

    Elev is loaded nightly from Elev_STG, which holds folkeskole pupils. A
    student on midlertidig kørsel to an ungdomsuddannelse was never in it, so
    the conversion used to reject the case — even though the municipality
    plainly knows the person: LOIS.CPR.PersonGeoView covers every citizen.

    What the earlier attempt got wrong was creating a SHELL: {cpr, adresse_id}
    and nothing else, a row no later process would ever fill in. This is the
    opposite — everything LOIS actually knows is written, and the fields it
    cannot know are left at the values that mean "unknown" rather than filled
    with guesses:

        adresseringsnavn            LOIS
        navne_adresse_beskyttelse   LOIS Beskyttelseskode = 1
        adresse_id                  LOIS, or the bevilling's own address when
                                    the register row is not in Adresse
        skolekode                   BefordringsData SkoleID, else 0
        matrikel_id                 left NULL — the API defaults it
        ungdomsuddannelse_id        not settable here; usp_sync_elev_matrikel_
                                    from_bevilling derives it from the
                                    bevilling
        klasseart, elevklassetrin,
        klassebetegnelse,
        institution,
        bopaelsdistrikt             "" — the school data nobody has for a
                                    student outside the folkeskole
        skoleafstand                NULL — no school, nothing to measure to
        kraever_genberegning        False — a walking distance needs a school
                                    at one end, and there is none

    A skolekode of 0 keeps them out of the school derivation, which is
    correct: usp_sync_elev_matrikel_from_bevilling requires a non-zero
    skolekode before it will take a folkeskole matrikel from a bevilling, and
    this student does not attend one.

    Without a LOIS row there is nothing to build from, and the case is still
    rejected — that is a person the municipality has no record of at all.

    The note goes on every kørselsrække of every bevilling on the case. This
    is the thinnest conversion the bot performs — a student built from a
    person register rather than a school register — and the fields it could
    not fill are exactly the ones a caseworker needs to supply. Saying so on
    the rows puts the case in the same KONVERTERING-PPR list as the rest
    instead of leaving it to be noticed.
    """

    if not lois_person:
        raise BusinessError(
            f"Student {person_ssn} (PPR case {ppr_case_id}) is in neither "
            "Elev nor LOIS.CPR.PersonGeoView. The nightly student load does "
            "not know this CPR, and neither does the municipality's own "
            "person register — they have most likely moved away. Needs "
            "manual review before the bevilling can be converted."
        )

    lois_adresse_id = str(lois_person.get("adresse_id") or "").strip()
    bevilling_adresse_id = next(
        (
            str(b.get("adresse_id")).strip()
            for b in bevillinger
            if b.get("adresse_id")
        ),
        "",
    )

    # LOIS says where they live NOW, which is what Elev.adresse_id means. The
    # bevilling's own address is the fallback: it is historical, but it is
    # guaranteed to be a row in Adresse because the queue phase resolved it
    # against that very table.
    adresse_id = lois_adresse_id or bevilling_adresse_id
    kilde = "LOIS"

    if lois_adresse_id and not _adresse_findes(api_endpoint, headers, lois_adresse_id):
        adresse_id = bevilling_adresse_id
        kilde = "bevillingens egen adresse (LOIS-adressen findes ikke i Adresse)"

    if not adresse_id:
        raise BusinessError(
            f"Student {person_ssn} (PPR case {ppr_case_id}) is not in Elev, "
            "and neither LOIS nor the bevilling supplies an address to "
            "create them with — POST /citizen/create_elev requires one. "
            "Needs manual review."
        )

    payload = {
        "cpr": _cpr_cifre(person_ssn),
        "adresseringsnavn": lois_person.get("adresseringsnavn"),
        "adresse_id": adresse_id,
        "navne_adresse_beskyttelse": bool(
            lois_person.get("navne_adresse_beskyttelse")
        ),
        "skolekode": _skolekode_fra_rows(bevillinger),
    }

    response = requests.post(
        f"{api_endpoint}/citizen/create_elev",
        headers=headers,
        json=payload,
        timeout=30,
    )

    if not response.ok:
        raise ProcessError(
            f"Failed to create Elev for {person_ssn} (PPR case {ppr_case_id}) "
            f"from LOIS: {response.status_code} — {response.text}"
        )

    logger.warning(
        "  Student %s (PPR case %s) was not in Elev and has been created "
        "from LOIS: navn %r, adresse_id %s (%s), skolekode %s. Klassetrin, "
        "klasseart and institution are blank — LOIS does not hold them, and "
        "the nightly load will not fill them in for a student outside the "
        "folkeskole.\n",
        person_ssn,
        ppr_case_id,
        payload["adresseringsnavn"] or "(ukendt)",
        adresse_id,
        kilde,
        payload["skolekode"],
    )

    note = _konverterings_note(
        "eleven er oprettet ud fra folkeregisteret",
        "Eleven fandtes ikke i elevdata, fordi det natlige elevtræk kun "
        "dækker folkeskolen. Bevillingen er derfor konverteret på grundlag "
        "af folkeregisteret (LOIS).",
        f"Hentet derfra: navn og adresse ({_adresse_id_kilde_tekst(kilde)}).",
        "Skolekode: "
        + (
            str(payload["skolekode"])
            + " fra de gamle data."
            if payload["skolekode"]
            else "ingen — de gamle data oplyste ingen brugbar skolekode."
        ),
        "Klassetrin, klasseart, klassebetegnelse, institution og "
        "bopælsdistrikt står tomme, og skoleafstand er ikke beregnet. De "
        "oplysninger findes ingen steder for en elev uden for folkeskolen.",
        "Gennemgå bevillingen, og udfyld det, der mangler.",
    )

    return adresse_id, note


def create_bevilling(
    ppr_case_id: str,
    person_ssn: str,
    bevillinger: list[dict],
    lois_person: dict | None = None,
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
    # Real staff, not seeded reference data — so an empty table is a
    # deployment problem, not a data problem, and it would otherwise surface
    # as every single case failing on its Author.
    if not sagsbehandlere:
        raise ProcessError(
            "The Sagsbehandler table is empty. It holds real staff and is "
            "not seeded, so it has to be populated before a conversion runs "
            "— every bevilling takes its caseworker from the row's Author."
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

    # Skolematrikel map: skolekode (string) → EVERY matrikel with that code.
    # /lookup/skolematrikel returns {"id": matrikel_id, "label": navn, "skolekode": ...},
    # and SkoleID from BefordringsData matches the skolekode column.
    #
    # A list, not one id. Several schools are split across two sites that
    # share a skolekode — Stensagerskolen is 751903 at both Janesvej and
    # Stensagervej — and keying on the code alone silently kept whichever
    # came last, sending every student to the same site. _vaelg_matrikel
    # picks between them on SkolensAdresse.
    skolematrikel_map: dict[str, list[dict]] = {}

    for item in skolematrikler:
        if item.get("skolekode") is None:
            continue

        skolematrikel_map.setdefault(str(item["skolekode"]).strip(), []).append(item)

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

    stamdata = stamdata_response.json()

    if stamdata is None:
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
        # ... but the municipality still knows them. LOIS.CPR.PersonGeoView
        # covers every citizen, so the student is created from that instead of
        # the case being rejected. Only a person LOIS has never heard of is
        # still a BusinessError — see _opret_elev_fra_lois.
        elev_adresse_id, elev_note = _opret_elev_fra_lois(
            api_endpoint,
            headers,
            person_ssn,
            ppr_case_id,
            lois_person,
            bevillinger,
        )
    else:
        elev_note = None
        # Where the student lives now, per the nightly Elev load. Every
        # converted bevilling's address is checked against this — see the
        # candidate pick in the loop below. None when the nightly run has not
        # resolved an address for them yet, which switches the check off
        # rather than failing the case.
        elev_adresse_id = stamdata.get("adresse_id")

        logger.info(
            "Student %s exists in Elev. Current adresse_id: %s\n",
            person_ssn,
            elev_adresse_id or "(ingen)",
        )

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

        # --- Which of the bevilling's addresses goes on it ---
        #
        # queue_handler passes every distinct address the bucket's rows
        # resolved to, in row order. They disagree more often than they look
        # like they should: a klub row names the klub rather than the home, and
        # a bucket spanning a move holds both the old address and the new one.
        #
        # The rule is the student's own address decides. If any row resolved to
        # where they live now, that is the bevilling's address — the data is
        # correct and the status engine leaves it alone. If none did, the first
        # resolving row is kept, it will not equal Elev.adresse_id, and
        # usp_recalculate_bevilling_status raises genbehandling by itself.
        # Nothing has to force that flag: a wrong address IS the mismatch it
        # looks for.
        #
        # The other rows are not lost — each is its own kørselsrække, and a
        # klub row carries its raw values in its comment.
        kandidater = bevilling.get("adresse_id_kandidater") or []
        adresse_id = bevilling.get("adresse_id")

        if elev_adresse_id and elev_adresse_id in kandidater:
            adresse_id = elev_adresse_id

        if len(kandidater) > 1:
            logger.info(
                "  Bevilling %d/%d: kørselsrækkerne peger på %d forskellige "
                "adresser (%s). Valgt: %s (%s).\n",
                i,
                len(bevillinger),
                len(kandidater),
                ", ".join(str(k) for k in kandidater),
                adresse_id,
                "elevens egen adresse"
                if adresse_id == elev_adresse_id
                else "ingen af dem matcher elevens adresse — går til genbehandling",
            )

        # --- Resolve bevilling-level lookup IDs ---
        skole_id = str(bevilling.get("SkoleID") or "").strip()
        matrikel_kandidater = skolematrikel_map.get(skole_id, [])
        matrikel_id, matrikel_note = _vaelg_matrikel(
            matrikel_kandidater,
            bevilling.get("skole_kandidater")
            # Older queue items predate skole_kandidater; fall back to the
            # bevilling-level pair so they still convert.
            or [[bevilling.get("SkolensAdresse"), bevilling.get("SkoleNavnBefordring")]],
            bevilling.get("bucket"),
            skolematrikler,
            ppr_case_id,
        )

        if matrikel_note and "skole sat manuelt" in matrikel_note:
            logger.warning(
                "  Skolekode %s: skolen er sat manuelt fra "
                "MATRIKEL_OVERRIDES — %s. Kørselsrækkerne får en kommentar.\n",
                skole_id,
                next(
                    (k.get("label") for k in skolematrikler if k["id"] == matrikel_id),
                    matrikel_id,
                ),
            )
        elif matrikel_note:
            logger.warning(
                "  Skolekode %s: afdelingen kunne ikke afgøres, og "
                "bevillingen er udløbet — gættet. Kørselsrækkerne får en "
                "kommentar.\n",
                skole_id,
            )

        if len(matrikel_kandidater) > 1:
            logger.info(
                "  Skolekode %s har %d matrikler — valgt %s ud fra "
                "rækkernes skolekolonner.\n",
                skole_id,
                len(matrikel_kandidater),
                next((k.get("label") for k in matrikel_kandidater
                      if k["id"] == matrikel_id), "?"),
            )

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

            # The date range, checked HERE rather than by the API after the
            # bevilling exists. The backend rejects gyldig_fra > gyldig_til
            # with a 400, and discovering that during the POST loop leaves a
            # bevilling created with no kørselsrækker — which the retry then
            # adopts and fails on again, every time. Same reasoning as the
            # lookups above: a bad value must fail the case before anything is
            # written.
            #
            # A safety net rather than the normal path: queue_handler swaps
            # reversed dates on every case, open or closed, so an item built
            # by the current code never arrives here inverted. One queued by
            # an older version still could, and failing before the write is
            # far better than a 400 halfway through.
            fra = _parse_date(kr.get("BevillingFra"))
            til = _parse_date(kr.get("BevillingTil"))

            if fra and til and fra > til:
                raise BusinessError(
                    f"Kørselsrække on PPR case {ppr_case_id} has "
                    f"BevillingFra {fra} after BevillingTil {til}, which the "
                    "API rejects. queue_handler swaps these, so this item was "
                    "most likely queued before that existed — re-run --queue "
                    "to rebuild it."
                )

            koersel_payload = {
                "gyldig_fra": kr.get("BevillingFra"),
                "gyldig_til": kr.get("BevillingTil"),
                "tidspunkt_id": tidspunkt_id,
                "befordringstype_id": befordringstype_id,
                "rutetype_id": rutetype_id,
                "bevilget_koereafstand_pr_vej": kr.get("BevilgetKoereAfstand") or None,
                "kommentar": _extend_kommentar(
                    _extend_kommentar(kr.get("Kommentar"), matrikel_note),
                    elev_note,
                ),
                "dag_ids": [alle_dage_id],
                "tillaeg_ids": tillaeg_ids,
            }

            koersel_payload = {k: v for k, v in koersel_payload.items() if v is not None}

            prepared.append((koersel_payload, rutetype_navn, kr))

        bevilling_payload = {
            # Per bevilling, not per case: a student who moved has older
            # bevillinger at the previous address. process_item has already
            # rejected the case if any of these is missing.
            "adresse_id": adresse_id,
            "matrikel_id": matrikel_id,
            "hjemmel_id": hjemmel_id,
            # From this bevilling's own Author, not one owner for the whole
            # run: the column is bevilling-level, and two bevillinger on one
            # case can have been written by different people.
            "sagsbehandler_id": _vaelg_sagsbehandler(
                bevilling.get("Author"), sagsbehandlere, ppr_case_id
            ),
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
