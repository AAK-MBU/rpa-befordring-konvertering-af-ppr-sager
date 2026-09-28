"""Module for general configurations of the process"""

MAX_RETRY = 10

# ----------------------
# Queue population settings
# ----------------------
MAX_CONCURRENCY = 100  # tune based on backend capacity

# --- Manuelle skolematrikel-afgørelser -------------------------------------
#
# PPR-sags-ID -> den skolematrikel bevillingen skal oprettes på.
#
# Nogle sager kan ikke placeres ud fra rækkerne alene. Et eksempel er en sag
# med UDELUKKENDE klubkørsel (skole -> klub -> hjem): ingen række navngiver
# skolen i SkolensAdresse/SkoleNavnBefordring, fordi begge kolonner beskriver
# klubturen, og en skolekode med to afdelinger kan derfor ikke afgøres.
#
# Her afgør et menneske det i stedet. Konverteringen opretter bevillingen på
# den valgte matrikel og skriver en KONVERTERING-PPR-kommentar på hver
# kørselsrække, så sagsbehandleren kan rette bevillingen bagefter.
#
# "matrikel" skal staves NØJAGTIGT som labelen i /lookup/skolematrikel — fx
# "Stensagerskolen (Stensagervej)". En forkert label stopper konverteringen
# med en ProcessError, så en tastefejl ikke bliver til en forkert skole.
#
# "skolekode" er valgfri. Er den sat, gælder afgørelsen kun den bevilling på
# sagen, der har netop den SkoleID — så en sag med to bevillinger på hver sin
# skole ikke bliver ramt begge steder.
MATRIKEL_OVERRIDES: dict[str, dict[str, str]] = {
    "PPR-2017-056295-006": {
        "matrikel": "Stensagerskolen (Stensagervej)",
        "skolekode": "751903",
        "begrundelse": (
            "Sagen er kun klubkørsel (skole -> klub -> hjem), så ingen "
            "række navngiver skolen. Sagsbehandleren har bekræftet "
            "Stensagervej, og skole -> klub-rækkens ElevensAdresse siger "
            "det samme."
        ),
    },
}

MAX_RETRIES = 3  # transient failure retries per item
RETRY_BASE_DELAY = 0.5  # seconds (exponential backoff)


# --- Resolved-address cache ---------------------------------------------
#
# Address resolution is the slow part of the queue phase: one or more API
# calls per distinct address, against ~3700 rows of which the overwhelming
# majority resolve first time and never change. Re-running to look at a
# handful of failures should not mean paying for all of them again.
#
# Successes only. A failure is never cached, so every re-run retries exactly
# the addresses still being worked on — which is the point.
#
# Written as it goes, so a run that dies half way keeps what it had.
#
# DELETE THE FILE after changing the matching rules. A cached hit skips the
# matcher entirely, so an entry written under the old rules would survive a
# change meant to correct it. Set to None to switch the cache off.
RESOLVED_ADDRESS_CACHE = "resolved_addresses.csv"


# --- Closed PPR cases ----------------------------------------------------
#
# BefordringsData does not say whether a PPR case is still open, so the list
# is exported from ESDH by hand. Two columns: "Sags ID" (matching CaseID) and
# "Status", where the rows that count say "Lukket".
#
# What it is used for: a closed case cannot be edited, so nobody can ever fix
# an address it got wrong — and its bevilling is not active, so an imprecise
# address costs nothing. Rather than lose the row, a closed case whose address
# will not resolve is converted onto the student's current address from LOIS,
# and its kørselsrækker say so.
#
# Missing file means no case is treated as closed, and those addresses fail as
# they did before.
CLOSED_CASES_CSV = "Lukkede foranstaltningsmapper.csv"


# --- Manual address corrections ------------------------------------------
#
# Phrase-level search and replace on the source address, for wordings no rule
# can derive. The case it was built for is an abbreviated street name:
#
#     Find                    Erstat
#     I. Christensens Gade    Inger Christensens Gade
#
# Two columns, "Find" and "Erstat". Edit the file and re-run — no code change,
# and nothing else has to be touched. Deleting resolved_addresses.csv first is
# only necessary if the address had already resolved to something wrong.
#
# Matched case-insensitively and across any amount of whitespace, so
# "I.  Christensens  Gade" hits the same row. Applied to every address, not
# only failing ones: a correction is a correction.
#
# Missing file means no replacements, which is the behaviour without it.
ADDRESS_REPLACEMENTS_CSV = "adresse_erstatninger.csv"


# --- Unresolved addresses, for the caseworkers -----------------------------
#
# Every address the run could not resolve, written out as a worklist:
# PPR case, CPR, the address as BefordringsData wrote it, and where CPR has
# that student living.
#
# Overwritten on every run, because it is a snapshot of THIS run's failures —
# an address fixed at source should disappear from it, not linger.
#
# Set to None to switch it off.
UNRESOLVED_ADDRESS_CSV = "uloeste_adresser.csv"

# The same worklist as a spreadsheet, which is what it actually gets opened
# in. Filterable header, frozen top row, readable column widths — none of
# which a CSV can carry.
#
# Both are written; set either to None to switch that one off.
UNRESOLVED_ADDRESS_XLSX = "uloeste_adresser.xlsx"
