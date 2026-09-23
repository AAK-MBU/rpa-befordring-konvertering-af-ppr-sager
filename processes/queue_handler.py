"""Module to handle queue population"""

import asyncio
import calendar
import csv
import json
import logging
import os
import re
from datetime import date
from itertools import groupby
from pathlib import Path

import pyodbc
import requests
from automation_server_client import Workqueue
from mbu_rpa_core.database.connection import RPAConnection
from mbu_rpa_core.exceptions import ProcessError

from helpers import config
from processes.bevilling_creation import get_api_credentials

logger = logging.getLogger(__name__)


# LOIS, on server 29. Same env var and same view as rpa-befordring-nightly-runs,
# which reads CPR.PersonGeoView every night to resolve Elev.adresse_id.
#
# Used here for diagnostics only: when a legacy address cannot be matched, the
# warning says where CPR has that student living, which is the correction a
# caseworker would otherwise look up by hand. Unset simply means that line is
# missing from the log — it is never required for a conversion, and the
# BefordringsData connection is a separate thing entirely, fetched from
# RPAConnection at runtime.
CONN_STRING_SERVER29 = os.getenv("DBCONNECTIONSTRINGSERVER29")


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

# A four-digit postcode and town sitting at the END of a part that does not
# start with one — i.e. a postcode the source failed to put after a comma.
# Requires a town after the digits, so a four-digit house number is not
# mistaken for a postcode.
_EMBEDDED_POSTCODE = re.compile(r"^(.*\S)\s*[.,]?\s+(\d{4}\s+\S.*)$")

# A zero-padded house number or floor: "Borresøvej 041" -> 41, "02 tv" -> 2 tv.
# DAR's canonical husnummer is 1-3 digits with NO leading zeros, so a padded
# number in the source can only ever mean the unpadded one — there is no
# distinct address to confuse it with.
#
# Limited to runs of at most three digits on purpose. A four-digit run is
# postcode-shaped, and Denmark does have postcodes beginning with zero (0800,
# and the 0900-0999 København C range), which this must never touch. _canon is
# not applied to the postcode component either, so that is two guards.
_PADDED_NUMBER = re.compile(r"\b0+(\d{1,2})\b")

# Anything outside the characters a Danish address is actually written with.
# Used only to decide whether an unresolved address should be logged as a
# repr as well: a zero-width space, a soft hyphen or a decomposed "å" makes a
# string that cannot be matched and cannot be seen either, and printing the
# text alone leaves nothing to go on.
_SUSPECT_CHARS = re.compile(r"[^\w\s,.\-/]", re.UNICODE)


def _components(tekst: str | None) -> list[str]:
    """Split an address into normalised comma-separated parts.

    Whitespace runs are collapsed and everything is casefolded, so
    "Kærlundvej  16" and "KÆRLUNDVEJ 16" compare equal.

    One repair is made on the way: the legacy data sometimes ends the floor
    with a period rather than a comma —

        Rosenhøj Bakke 20, 3. tv.  8260 Viby J
                                ^ should be a comma

    which leaves "3. tv. 8260 viby j" as a single part. That part then has to
    turn up among the register's middle parts, where it never will, and with
    no postcode at its head the address may be dropped outright. A four-digit
    postcode followed by a town is unmistakable wherever it sits, so the part
    is split there and the stray period trimmed.

    Only the LAST part is repaired, which is where a mis-punctuated postcode
    always lands, and only when it does not already begin with one. Nothing
    correctly written is touched.
    """

    parts = [
        " ".join(part.split()).casefold()
        for part in str(tekst or "").split(",")
        if part.strip()
    ]

    if parts and not _POSTCODE.match(parts[-1]):
        embedded = _EMBEDDED_POSTCODE.match(parts[-1])

        if embedded:
            head = embedded.group(1).rstrip(".,").strip()
            parts = parts[:-1] + ([head] if head else []) + [embedded.group(2)]

    return parts


def _canon(part: str | None) -> str:
    """Canonical form of ONE address component, for comparison only.

    The two systems punctuate the same address differently, and every
    difference seen so far is purely typographic:

        floor and door   BefordringsData  "1 th"     Adresse  "1. th"
        house number     BefordringsData  "31 c"     Adresse  "31C"

    Dropping every period and every space collapses both pairs onto one
    string ("1th", "31c"). Deliberately blunt: classifying the parts properly
    would mean parsing Danish address conventions, and this only has to decide
    whether two spellings name the same place.

    Blunt is safe here because ambiguity is already refused. An address is
    accepted only when exactly ONE candidate matches, so two register rows
    that canonicalise alike are reported as ambiguous and sent to manual
    follow-up rather than guessed between.

    NOT applied to the postcode component: _postcode_of matches on a word
    boundary after the four digits, which stripping spaces would destroy.
    """

    # Order matters: the zero has to be stripped while it is still recognisable
    # as LEADING. Once the spaces are gone, "vestergade 041" is "vestergade041"
    # and the zero is just a digit in the middle of a string.
    samlet = " ".join(str(part or "").split())

    return _PADDED_NUMBER.sub(r"\1", samlet).replace(" ", "").replace(".", "")


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

    if _canon(candidate[0]) != _canon(source[0]):
        return False

    # On the raw components: _postcode_of needs the space after the digits.
    if _postcode_of(candidate) != _postcode_of(source):
        return False

    # Every middle part of the source, in order, somewhere in the candidate's.
    remaining = iter(_canon(part) for part in candidate[1:-1])

    return all(_canon(part) in remaining for part in source[1:-1])


# Addresses the source writes in a form no amount of general normalisation can
# reach, mapped to what they are in the register.
#
# The case this exists for is "Center for Børne- og Ungehjem": the source
# writes the home's own name in front of the street —
#
#     Toppen, Årslev Møllevej 19, 8220 Brabrand
#
# and the name becomes the first comma-component, i.e. what the matcher takes
# for the street. Every prefix is then built from "Toppen" and nothing can
# match. The homes are a known, finite list, so naming them here is both
# simpler and safer than guessing which leading components are not streets.
#
# Matched as a substring through _fold, so case, spacing and æ/ø/å spelling do
# not matter — but never across a longer house number, so "Årslev Møllevej 19"
# does not swallow "Årslev Møllevej 190".
_ADRESSE_OVERRIDES: tuple[tuple[str, str], ...] = (
    ("Årslev Møllevej 19", "Årslev Møllevej 19, 8220 Brabrand"),
)


def _override_adresse(tekst: str | None) -> str | None:
    """The canonical address for a known special case, or None."""

    haystack = _fold(tekst)

    for marker, kanonisk in _ADRESSE_OVERRIDES:
        naal = _fold(marker)
        start = haystack.find(naal)

        if start == -1:
            continue

        slut = start + len(naal)

        # "19" must not be the front of "190".
        if slut < len(haystack) and haystack[slut].isdigit():
            continue

        return kanonisk

    return None


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

    raa = row.get("ElevensAdresse")

    # A known special case is replaced wholesale before parsing: what makes
    # these unmatchable is the SHAPE of the string, so there is nothing for
    # the component logic to work with.
    components = _components(_override_adresse(raa) or raa)

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


# "31 c" <-> "31c" — a house-number letter, written with or without a space.
_HOUSE_LETTER_SPACED = re.compile(r"(\d)\s+([a-zæøå])$")
_HOUSE_LETTER_JOINED = re.compile(r"(\d)([a-zæøå])$")

# Initials in a street name, written apart or together: "M. P. Hansens Vej"
# and "M.P. Hansens Vej" are the same road. Both directions are needed, since
# either side may be the one with the space.
#
# The lookahead is what keeps this off ordinary words: it fires only where a
# single letter and a period are followed by ANOTHER single letter and period,
# so "M. P. Hansens" is rewritten and "P. Hansens" alone is left exactly as it
# is. Without it, "m. p. hansens vej" would collapse to "m.p.hansens vej",
# which matches nothing.
_INITIAL_SPACED = re.compile(r"\b([a-zæøå])\.\s+(?=[a-zæøå]\.)")
_INITIAL_JOINED = re.compile(r"\b([a-zæøå])\.(?=[a-zæøå]\.)")


def _rewrite_until_stable(pattern: re.Pattern, replacement: str, tekst: str) -> str:
    """Apply a rewrite repeatedly until it stops changing anything.

    Each pass rewrites one initial, because the lookahead that makes the rule
    safe also consumes the context the next one needs. Three initials take
    three passes.
    """

    forrige = None

    while forrige != tekst:
        forrige = tekst
        tekst = pattern.sub(replacement, tekst)

    return tekst


# "1 th" / "1. th" / "st tv" / "st. tv" — a floor token, with or without its
# period. kl and kld are kælder (basement).
_FLOOR_PREFIX = re.compile(r"^(\d+|st|kl|kld)\.?\s+(.*)$")


def _dedupe(values: list[str]) -> list[str]:
    """Order-preserving de-duplication."""

    seen: list[str] = []

    for value in values:
        if value not in seen:
            seen.append(value)

    return seen


def _street_variants(street: str) -> list[str]:
    """Spellings of the street component to try against the register.

    _canon makes COMPARISON tolerant of the space in a house-number letter,
    but the search itself is a prefix LIKE against adresse_tekst, so the
    prefix has to be spelled the way the register spells it. Both directions
    are generated because either side may be the one with the space:

        "Egå Mosevej 31 c"  ->  also try  "egå mosevej 31c"
        "Egå Mosevej 31C"   ->  also try  "egå mosevej 31 c"
    """

    varianter = [
        street,
        _HOUSE_LETTER_SPACED.sub(r"\1\2", street),
        _HOUSE_LETTER_JOINED.sub(r"\1 \2", street),
    ]

    # A zero-padded house number has no fallback: the padding sits in the
    # street component, so EVERY prefix built from it is wrong, including the
    # street-only one. Unlike a padded floor, which the street-only prefix
    # still finds, this has to be spelled correctly or the address is lost.
    varianter += [_PADDED_NUMBER.sub(r"\1", v) for v in list(varianter)]

    # Same story for initials: "M. P. Hansens Vej 14" against the register's
    # "M.P. Hansens Vej 14" compares equal once _canon has removed the spaces
    # and periods, but the search never returns the row for it to compare.
    for variant in list(varianter):
        varianter.append(_rewrite_until_stable(_INITIAL_SPACED, r"\1.", variant))
        varianter.append(_rewrite_until_stable(_INITIAL_JOINED, r"\1. ", variant))

    return _dedupe(varianter)


def _floor_variants(part: str) -> list[str]:
    """Spellings of a floor/door component, with and without the period.

    Only needed to keep a large building inside the 15-row search cap: the
    street-only prefix would find it anyway, but a block with more than 15
    flats pushes the wanted row out of the results, and it looks absent.
    """

    match = _FLOOR_PREFIX.match(part)

    if not match:
        return [part]

    floor, rest = match.group(1), match.group(2)

    bar = _PADDED_NUMBER.sub(r"\1", floor)

    return _dedupe([
        part,
        f"{floor}. {rest}", f"{floor} {rest}",
        f"{bar}. {rest}", f"{bar} {rest}",
    ])


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

    Each is generated for every plausible spelling of the street, and of the
    floor, since the search cannot find what it does not spell the same way.
    The floor+street prefixes all come before the street-only ones, so the
    most selective search still runs first.
    """

    streets = _street_variants(source[0])

    prefixes: list[str] = []

    if len(source) > 2:
        for street in streets:
            for floor in _floor_variants(source[1]):
                prefixes.append(f"{street}, {floor},")

    prefixes.extend(f"{street}," for street in streets)

    return _dedupe(prefixes)


# What a source address looks like once the house number is taken off, so the
# register can be probed for the street itself: "hørret byvej 15" -> "hørret byvej".
_HOUSE_NUMBER_TAIL = re.compile(r"\s*\d+\s*[a-zæøå]?$")


def _lois_adresser(cprs: list[str]) -> dict[str, str]:
    """cpr -> adresse_id from LOIS.CPR.PersonGeoView. Empty dict when unavailable.

    The same view and the same key (PNR_0, ten digits, no dash) that
    rpa-befordring-nightly-runs uses to resolve Elev.adresse_id every night.
    Where a legacy address cannot be matched, this says where CPR thinks the
    student actually lives — which is the correction a caseworker would
    otherwise have to look up by hand, one student at a time.

    Diagnostic only. Every failure is swallowed, including the env var being
    unset: no LOIS means one missing hint in a warning, never a failed
    conversion.

    Chunked at 900 for the same reason the nightly run chunks: SQL Server caps
    a statement at 2100 parameters.
    """

    if not CONN_STRING_SERVER29:
        logger.warning(
            "DBCONNECTIONSTRINGSERVER29 is not set, so the unresolved "
            "addresses below cannot say where CPR has these students living. "
            "Set it to the same value rpa-befordring-nightly-runs uses. The "
            "conversion is unaffected.\n"
        )

        return {}

    rene = sorted({
        cifre
        for cifre in ("".join(c for c in str(cpr or "") if c.isdigit()) for cpr in cprs)
        if len(cifre) == 10
    })

    if not rene:
        return {}

    fundet: dict[str, str] = {}

    try:
        with pyodbc.connect(CONN_STRING_SERVER29) as conn:
            cursor = conn.cursor()

            for offset in range(0, len(rene), 900):
                chunk = rene[offset:offset + 900]
                placeholders = ",".join("?" for _ in chunk)

                cursor.execute(
                    f"""
                    SELECT [PNR_0], CONVERT(NVARCHAR(36), [AdresseId])
                    FROM   [LOIS].[CPR].[PersonGeoView]
                    WHERE  [PNR_0] IN ({placeholders})
                    AND    [AdresseId] IS NOT NULL
                    """,
                    chunk,
                )

                for cpr, adresse_id in cursor.fetchall():
                    if cpr and adresse_id:
                        fundet[str(cpr).strip()] = str(adresse_id).strip()
    except pyodbc.Error as exc:
        logger.warning(
            "Could not read LOIS.CPR.PersonGeoView for the unresolved "
            "addresses, so the log cannot show where CPR has these students "
            "living. The conversion is unaffected. %s\n",
            exc,
        )

        return {}

    return fundet


def _adresse_tekst(api_endpoint: str, headers: dict, adresse_id: str) -> str | None:
    """The register's spelling of one adresse_id. None when it cannot be read."""

    try:
        response = requests.get(
            f"{api_endpoint}/adresse/{adresse_id}",
            headers=headers,
            timeout=30,
        )

        if not response.ok:
            return None

        return (response.json() or {}).get("adresse_tekst")
    except requests.RequestException:
        return None


def _probe_register(
    api_endpoint: str,
    headers: dict,
    source: list[str],
    postnummer: str | None,
) -> list[str]:
    """What the register actually holds near an address that would not resolve.

    Diagnostic only, and only for addresses that already failed, so it costs
    one extra request per failure and none at all on a clean run.

    The searches the matcher runs all end in a comma — that is what stops
    "Hørret Byvej 15," matching "Hørret Byvej 150," — which also means that
    when the register has 15A but no bare 15, every one of them returns
    nothing and the log can only say "0". Dropping the comma asks the far more
    useful question: what IS there? For that address the answer is 15A, 15C
    and 15D, which turns "could not be matched" into "the caseworker left the
    letter off".

    Falls back to the street without its house number, so a wrong number still
    shows the street exists. Never raises: a failed probe means one missing
    hint, not a failed conversion.
    """

    # Every spelling the matcher itself tried, not just the raw one. Probing
    # only the source's own wording is how "Borresøvej 041" ended up reporting
    # the neighbours of Borresøvej 10: the raw street found nothing, so it fell
    # straight through to the bare street name, where alphabetical order starts
    # at 10 — while "borresøvej 41" would have found the building.
    probes = list(_street_variants(source[0]))

    # Last resort only: the street with no house number at all. Answers "does
    # this street even exist", at the cost of listing addresses that have
    # nothing to do with the one being looked for.
    uden_nummer = _HOUSE_NUMBER_TAIL.sub("", source[0]).strip()

    if uden_nummer and uden_nummer not in probes:
        probes.append(uden_nummer)

    for probe in probes:
        if len(probe) < 2:
            continue

        try:
            response = requests.get(
                f"{api_endpoint}/adresse/search",
                params={"q": probe, "postnummer": postnummer, "limit": 25},
                headers=headers,
                timeout=30,
            )

            if not response.ok:
                continue

            fundet = [
                row.get("adresse_tekst")
                for row in (response.json() or [])
                if row.get("adresse_tekst")
            ]

            if fundet:
                return fundet
        except requests.RequestException:
            continue

    return []


def _vaelg_via_cpr(
    entry: dict,
    lois_adresse_id: dict[str, str],
) -> tuple[str, str] | None:
    """Settle an ambiguous address with CPR, or None when it cannot be settled.

    Several register rows fitting the source equally well usually means the
    register holds both an access address and its unit address — "Øster
    Kringelvej 25" and "Øster Kringelvej 25, st." are the same dwelling
    written twice — and no rule about the source text can separate them. CPR
    can: it says which row the student is actually registered at.

    Deliberately narrow:

      only an AMBIGUITY, never a miss   picking from candidates that already
                                        matched keeps the answer consistent
                                        with the source. Where nothing
                                        matched, CPR's address is not among
                                        them, and taking it would invent an
                                        address the source never supported —
                                        exactly the case a caseworker must
                                        look at.

      only on agreement                 several students can share one legacy
                                        address. If their CPR addresses point
                                        at different candidates, that is a new
                                        disagreement, not an answer.
    """

    kandidater = dict(entry.get("kandidater") or [])

    if len(kandidater) < 2:
        return None

    valgte = {
        lois_adresse_id[cpr]
        for cpr in entry.get("cprs") or []
        if lois_adresse_id.get(cpr) in kandidater
    }

    if len(valgte) != 1:
        return None

    adresse_id = valgte.pop()

    return adresse_id, kandidater[adresse_id]


def _unresolved_line(
    key: tuple[str, ...],
    kilde: str,
    count: int,
    forsoeg: list[tuple[str, int, int]],
    naboer: list[str],
    cprs: list[str] | None = None,
    lois: list[tuple[str, str]] | None = None,
    kandidater: list[tuple[str, str]] | None = None,
) -> str:
    """One block per address that could not be resolved, with enough to act on.

    "0 candidate(s)" is not diagnosable. Nor is the normalised text on its own:
    it hides how the address was written, and says nothing about what was
    looked for or what the register had instead. So the block carries the
    source verbatim, every search that was run with what it returned, and the
    addresses that actually exist on that street.

    That is what separates the two kinds of failure. A matcher that cannot
    cope with the spelling shows searches returning rows that were then
    rejected; bad source data shows searches returning nothing while the
    register plainly holds the neighbours.

    A repr is added ONLY when the text contains a character a Danish address is
    not written with — a zero-width space, a soft hyphen or a decomposed "å"
    breaks matching while looking perfectly normal on screen, and without it
    there is nothing to see.
    """

    normaliseret = " | ".join(key)

    linjer = [
        f"  {kilde}",
        f"      normaliseret : {normaliseret}",
    ]

    for prefix, raekker, matchede in forsoeg:
        linjer.append(
            f"      søgte på     : {prefix!r} → {raekker} række(r), {matchede} match"
        )

    # Only the source. normaliseret is joined with " | ", and the pipe would
    # trip the check on every single failure.
    if _SUSPECT_CHARS.search(kilde):
        linjer.append(f"      tegn         : {kilde!a}")

    if naboer:
        linjer.append(
            "      passer lige godt:" if count > 1 else "      registret har:"
        )
        linjer.extend(f"                     {n}" for n in naboer[:8])

        if len(naboer) > 8:
            linjer.append(f"                     … og {len(naboer) - 8} flere")
    else:
        linjer.append("      registret har: intet på den vej i det postnummer")

    # Where CPR says the student lives now. Not the answer — a bevilling is
    # granted at the address it was granted at, so a student who has moved
    # SHOULD differ — but it is the one other fact about this student's
    # address that exists, and it is usually the correction.
    if lois:
        linjer.append("      elev iflg. CPR:")
        linjer.extend(
            f"                     {cpr}: {tekst or '(kendes ikke i Adresse-tabellen)'}"
            for cpr, tekst in lois
        )
    elif cprs:
        linjer.append(f"      elev iflg. CPR: intet opslag for {', '.join(cprs)}")

    if count > 1:
        linjer.append(
            f"      => {count} adresser passer lige godt — kilden siger ikke hvilken"
        )
    elif naboer:
        linjer.append("      => adressen findes ikke som skrevet; se ovenstående")

    linjer.append("\n")
    linjer.append("------------------------------------------------------------------------------------------------------------------------------")

    return "\n".join(linjer)


_CACHE_FELTER = ("noegle", "adresse_id", "adresse_tekst", "kilde")


def _cache_path() -> Path | None:
    """Where the resolved-address cache lives, or None when switched off."""

    navn = getattr(config, "RESOLVED_ADDRESS_CACHE", None)

    return Path(navn) if navn else None


def _load_address_cache() -> dict[tuple[str, ...], tuple[str, str]]:
    """Addresses resolved by an earlier run: key -> (adresse_id, adresse_tekst).

    The queue phase resolves one address at a time against the API, and on a
    full run almost all of them succeed and keep succeeding. Re-running to
    look at the few that failed should not mean paying for the rest again.

    The key is stored as JSON rather than joined with a separator, because an
    address component can contain very nearly any punctuation and a separator
    that turns up inside a component would split it in the wrong place.

    Anything unreadable is skipped rather than fatal: this is a cache, and the
    worst a bad line can cost is one address resolved again.
    """

    path = _cache_path()

    if not path or not path.exists():
        return {}

    cache: dict[tuple[str, ...], tuple[str, str]] = {}

    try:
        with path.open(newline="", encoding="utf-8") as fil:
            for row in csv.DictReader(fil):
                try:
                    noegle = tuple(json.loads(row["noegle"]))
                except (TypeError, ValueError, KeyError):
                    continue

                adresse_id = (row.get("adresse_id") or "").strip()

                if noegle and adresse_id:
                    cache[noegle] = (adresse_id, row.get("adresse_tekst") or "")
    except OSError as exc:
        logger.warning("Could not read %s: %s\n", path, exc)

        return {}

    return cache


def _open_address_cache():
    """Append handle for the cache, with its header written if the file is new.

    Returned open, and written to as each address resolves rather than once at
    the end: a run over thousands of addresses that dies half way should keep
    what it had.
    """

    path = _cache_path()

    if not path:
        return None, None

    try:
        nyt = not path.exists() or path.stat().st_size == 0
        fil = path.open("a", newline="", encoding="utf-8")
        writer = csv.writer(fil)

        if nyt:
            writer.writerow(_CACHE_FELTER)

        return fil, writer
    except OSError as exc:
        logger.warning(
            "Could not open %s for writing, so this run will not be cached: "
            "%s\n",
            path,
            exc,
        )

        return None, None


def _resolve_adresse_ids(
    rows: list[dict],
) -> tuple[dict[tuple[str, ...], str], dict[str, str]]:
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
        (resolved, tekster) where resolved maps the address components ->
        adresse_id, and tekster maps adresse_id -> the register's own spelling
        of it. The second is for logging: a queue line showing what a source
        address matched is only useful if it names the address rather than its
        GUID.

        Addresses that resolve to nothing, or to more than one candidate, are
        omitted from both and logged; the bevilling using them is then
        rejected for manual follow-up, since Bevilling.adresse_id is NOT NULL.
    """

    api_endpoint, api_key = get_api_credentials()
    headers = {"X-API-Key": api_key}

    # key -> the address as BefordringsData wrote it. The key alone is the
    # normalised form, which is not what a caseworker would recognise and hides
    # exactly the punctuation a failure usually turns on.
    kilder: dict[tuple[str, ...], str] = {}

    # key -> the CPRs whose rows used this address. Only needed for failures,
    # where it is what lets the log say where CPR has that student living.
    cpr_pr_adresse: dict[tuple[str, ...], set[str]] = {}
    overskrevne = 0

    for row in rows:
        key = _address_key(row)

        if not key:
            continue

        if key not in kilder:
            kilder[key] = " ".join(str(row.get("ElevensAdresse") or "").split())

        if _override_adresse(row.get("ElevensAdresse")):
            overskrevne += 1

        cpr = str(row.get("CPR") or "").strip()

        if cpr:
            cpr_pr_adresse.setdefault(key, set()).add(cpr)

    keys = set(kilder)

    if overskrevne:
        logger.info(
            "%d row(s) use an address written in a known special form and "
            "were rewritten to the register's wording before matching — see "
            "_ADRESSE_OVERRIDES.\n",
            overskrevne,
        )

    cache = _load_address_cache()
    cache_fil, cache_writer = _open_address_cache()
    fra_cache = 0

    resolved: dict[tuple[str, ...], str] = {}
    # adresse_id -> the register's own spelling, so the queue log can show what
    # each source address actually matched rather than a bare GUID.
    tekster: dict[str, str] = {}
    unresolved: list[tuple] = []

    for key in sorted(keys):
        # Straight from an earlier run. Skips the searches entirely, which is
        # the whole point — on a re-run to inspect a handful of failures,
        # nearly every address takes this branch.
        if key in cache:
            adresse_id, adresse_tekst = cache[key]
            resolved[key] = adresse_id
            tekster[adresse_id] = adresse_tekst
            fra_cache += 1
            continue

        source = list(key)
        candidates: list[dict] = []
        # (prefix, rows the register returned, rows that then matched) — the
        # difference between the last two is what says whether the matcher or
        # the source data is at fault.
        forsoeg: list[tuple[str, int, int]] = []

        # Always sent with the search. Adresse is the whole of Denmark — the
        # nightly import reads AdresseDkGeoView with no municipality filter —
        # and the search orders alphabetically, which on a string whose next
        # characters are the postcode means the HIGHEST postcodes fall off the
        # end of the limit first. Without this, "Bøgebakken 2," comes back as
        # Greve, Roskilde and Køge, and 8462 Harlev J is never seen.
        # None rather than "" when absent: requests omits a None param, while an
        # empty string would be sent and rejected by the endpoint.
        postnummer = _postcode_of(source) or None

        for prefix in _search_prefixes(source):
            response = requests.get(
                f"{api_endpoint}/adresse/search",
                # limit well above the endpoint's combobox default: a single
                # large block of flats can exceed 15 on the street-only
                # prefix, and a truncated result looks like an absent address
                # rather than an ambiguous one.
                params={"q": prefix, "postnummer": postnummer, "limit": 200},
                headers=headers,
                timeout=30,
            )

            if not response.ok:
                raise ProcessError(
                    f"Address search failed for {prefix!r}: "
                    f"{response.status_code} — {response.text}"
                )

            raekker = response.json() or []

            candidates = [
                candidate
                for candidate in raekker
                if _matches(candidate.get("adresse_tekst"), source)
            ]

            forsoeg.append((prefix, len(raekker), len(candidates)))

            if candidates:
                break

        if len(candidates) == 1:
            adresse_id = candidates[0]["adresse_id"]
            adresse_tekst = candidates[0].get("adresse_tekst") or ""

            resolved[key] = adresse_id
            tekster[adresse_id] = adresse_tekst

            # Written now, not at the end: a run over thousands of addresses
            # that dies half way should keep what it had. Successes only —
            # a failure must be retried on the next run, which is exactly what
            # a re-run is usually for.
            if cache_writer is not None:
                cache_writer.writerow([
                    json.dumps(list(key), ensure_ascii=False),
                    adresse_id,
                    adresse_tekst,
                    kilder[key],
                ])
                cache_fil.flush()

            continue

        # Two different failures, two different things worth printing.
        #
        # Ambiguous (several matched): the candidates ARE the problem — the
        # source does not say which of them it means — so they are what to
        # show. Probing the street again would answer a question nobody asked.
        #
        # Nothing matched: there is nothing to show, so go and find out what
        # the register does hold nearby.
        if candidates:
            naboer = [
                candidate.get("adresse_tekst", "")
                for candidate in candidates
                if candidate.get("adresse_tekst")
            ]
        else:
            naboer = _probe_register(api_endpoint, headers, source, postnummer)

        unresolved.append({
            "key": key,
            "kilde": kilder[key],
            "count": len(candidates),
            "forsoeg": forsoeg,
            "naboer": naboer,
            "cprs": sorted(cpr_pr_adresse.get(key, set())),
            # Kept so an ambiguity can still be settled by CPR below. Ids, not
            # just the texts _unresolved_line prints.
            "kandidater": [
                (c["adresse_id"], c.get("adresse_tekst") or "")
                for c in candidates
                if c.get("adresse_id")
            ],
        })

    if unresolved:
        # Where CPR has these students living, for the caseworker who has to
        # correct the legacy address by hand. One query covering every failure
        # rather than one per address, and only for failures — a clean run
        # never touches LOIS.
        lois_adresse_id: dict[str, str] = {}
        lois_tekst: dict[str, str] = {}

        alle_cprs = [cpr for entry in unresolved for cpr in entry["cprs"]]
        lois_adresse_id = _lois_adresser(alle_cprs)

        # One call per DISTINCT address: several students behind the same
        # failing address usually share one.
        for adresse_id in sorted(set(lois_adresse_id.values())):
            tekst = _adresse_tekst(api_endpoint, headers, adresse_id)

            if tekst:
                lois_tekst[adresse_id] = tekst

        for entry in unresolved:
            entry["lois"] = [
                (cpr, lois_tekst.get(lois_adresse_id.get(cpr, ""), ""))
                for cpr in entry["cprs"]
                if cpr in lois_adresse_id
            ]

        # Settle what CPR can settle.
        #
        # An ambiguity means several register rows fit the source equally
        # well, and the source does not say which. Where the register holds
        # both an access address and its unit address — "Øster Kringelvej 25"
        # and "Øster Kringelvej 25, st." — that is the same dwelling written
        # twice, and no rule about the source text can separate them.
        #
        # CPR can: it says which of those rows the student is registered at.
        # Only ever used to choose BETWEEN candidates that already matched, so
        # the result is always an address consistent with the source, and only
        # when the CPRs behind the address agree on one. It never overrides a
        # clean single match and never invents one where nothing matched.
        stadig_uloeste = []

        for entry in unresolved:
            valgt = _vaelg_via_cpr(entry, lois_adresse_id)

            if valgt is None:
                stadig_uloeste.append(entry)
                continue

            adresse_id, adresse_tekst = valgt

            resolved[entry["key"]] = adresse_id
            tekster[adresse_id] = adresse_tekst

            if cache_writer is not None:
                cache_writer.writerow([
                    json.dumps(list(entry["key"]), ensure_ascii=False),
                    adresse_id,
                    adresse_tekst,
                    entry["kilde"],
                ])
                cache_fil.flush()

            logger.info(
                "  %s: %d adresser passede lige godt — valgt %r, fordi CPR "
                "har eleven boende der.\n",
                entry["kilde"],
                entry["count"],
                adresse_tekst,
            )

        unresolved = stadig_uloeste

    if cache_fil is not None:
        cache_fil.close()

    logger.info(
        "Resolved %d/%d distinct bevilling address(es) against the Adresse "
        "table (%d from %s, %d looked up).\n",
        len(resolved),
        len(keys),
        fra_cache,
        _cache_path() or "cache",
        len(resolved) - fra_cache,
    )

    if unresolved:
        logger.warning(
            "%d address(es) unresolved — the bevillinger using them will be "
            "rejected for manual follow-up:\n%s\n",
            len(unresolved),
            "\n".join(_unresolved_line(**entry) for entry in unresolved),
        )

    raise SystemExit("Manual stop")

    return resolved, tekster


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
        SELECT
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
    adresse_ids, adresse_tekster = _resolve_adresse_ids(rows)

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
        # (bucket, [(kilde, match)]) per bevilling — for the queue log below.
        adresse_log: list[tuple[str, list[tuple[str, str | None]]]] = []
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

            # (source address as BefordringsData wrote it, register address it
            # matched or None) — logged per queue item so the conversion can be
            # read back address by address. Kept out of the queue item itself:
            # it is for the run log, and ATS does not need it.
            adresse_opslag: list[tuple[str, str | None]] = []

            for row in bev_rows:
                kilde = " ".join(str(row.get("ElevensAdresse") or "").split())
                key = _address_key(row)
                kandidat = adresse_ids.get(key) if key else None

                if kandidat and kandidat not in bevilling_adresse_kandidater:
                    bevilling_adresse_kandidater.append(kandidat)

                opslag = (kilde, adresse_tekster.get(kandidat) if kandidat else None)

                if opslag not in adresse_opslag:
                    adresse_opslag.append(opslag)

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

            adresse_log.append((bev_key[0], adresse_opslag))

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

        # Every address this item carries, as BefordringsData wrote it and as
        # the Adresse table spells it back. The two systems punctuate the same
        # address differently (see _canon), so a match that looks wrong at a
        # glance usually is not — and one that IS wrong is only visible if both
        # sides are printed side by side. Logged per item rather than as one
        # table at the end so it reads in the same order the queue was built.
        linjer = []

        for bucket, opslag in adresse_log:
            linjer.append(f"    [{bucket}]")

            for kilde, match in opslag:
                linjer.append(f"      kilde: {kilde or '(tom)'}")
                linjer.append(f"      match: {match or '— INTET MATCH —'}")

        logger.info(
            "Kø-emne %d | PPR-sag %s | CPR %s | %d bevilling(er)\n%s\n",
            len(items),
            ppr_case_id,
            person_ssn,
            len(bevillinger),
            "\n".join(linjer),
        )

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
