# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

One-off migration bot. It reads the legacy befordring data held in `[RPA].[rpa].[BefordringsData]` and recreates it as bevillinger and kørselsrækker in *Befordringssystemet*, the new application, through that application's REST API. It runs at go-live, not on a schedule.

Based on [odense-rpa/process-template](https://github.com/odense-rpa/process-template), which is why `application_handler` carries a GUI lifecycle with empty bodies — there is no GUI to drive.

An earlier version also journalized PPR documents into GetOrganized. That was removed (`79fd8fb`), along with `helpers/case_handler.py` and `helpers/document_handler.py`. Nothing here talks to GetOrganized any more.

## Commands

```bash
uv sync

uv run ruff check .
uv run ruff format .

python main.py --queue      # read BefordringsData, group, populate workqueue
python main.py --process    # create bevillinger via the befordring API
python main.py --finalize   # stub
```

CI (`.github/workflows/check_version_number.yml`) fails any PR to `main` that does not raise `version` in `pyproject.toml`.

## Architecture

### `--queue` — `processes/queue_handler.py`

`retrieve_items_for_queue()` does the whole grouping job:

1. Reads `BefordringsData` from the RPA database (connection string from `RPAConnection(db_env="PROD").get_constant("DbConnectionString")`).
2. Resolves the address **each bevilling was granted against** to an `adresse_id`, via `/adresse/search` against the befordring application's own `Adresse` table. That table is a full copy of the municipality's register, refreshed nightly by `rpa-befordring-nightly-runs`, so this bot no longer reaches into LOIS and the conversion is API-only.

   `Bevilling.adresse_id` records where a given bevilling was granted, which is what `adresse_mismatch` later compares against the student's own. A student who moved has older bevillinger at the previous address, so it is resolved **per bevilling**, not per case.

   **Rows inside one bevilling can disagree**, and often do: a klub row names the klub where the others name the home, and a bucket spanning a move holds the old address and the new one. `queue_handler` therefore passes *every* distinct address the bucket's rows resolved to, in row order, as `adresse_id_kandidater`; `bevilling_creation` makes the choice, because only it has the `Elev` record.

   The rule is that the student's own address decides:

   - **Any candidate equals `Elev.adresse_id`** → that one goes on the bevilling. The data is right and the status engine leaves it alone.
   - **None does** → the first resolving row is kept. It will not equal `Elev.adresse_id`, so `usp_recalculate_bevilling_status` computes `adresse_mismatch = 1` and raises genbehandling **by itself**. Nothing forces the flag: a wrong address *is* the mismatch it looks for, which is why this needed no schema change.

   The student's current address comes from `view_Stamdata`, which the bot already calls to check the student exists. The view had to expose `e.adresse_id` for this (`adresse_tekst` alone cannot be compared — two identical strings are not a match). The rows not chosen are not lost: each is still its own kørselsrække, and a klub row carries its raw values in its comment.

   `Bevilling.adresse_id` is `NOT NULL`, so a bevilling whose address cannot be matched cannot be created. `process_item` rejects the whole case in that event rather than converting it partially — a case missing one of its bevillinger looks complete to a caseworker and is harder to spot than one that never arrived.
3. Groups rows into **one queue item per PPR case**, referenced by `CaseID`, so re-running `--queue` cannot duplicate.
4. Within a case, buckets rows by **when they apply** — see `_bucket_key()`.

`_KOERSELSRAEKKE_FIELDS` is the dividing line: those six columns vary per kørselsrække, everything else is bevilling-level and taken as the first non-NULL value across the group — nullable columns like `Revurdering` are often NULL on early rows and populated on a later one.

### `--process` — `processes/process_item.py` → `processes/bevilling_creation.py`

`process_item()` validates the payload and delegates. `create_bevilling()` does the API work:

1. Fetches five lookup lists once (`tidspunkter`, `koerselstyper`, `hjemler`, `sagsbehandlere`, `skolematrikel`) and builds case-insensitive name → id maps.
2. `GET /citizen/stamdata/{cpr}`. The student **must** already exist — a miss raises `BusinessError` and parks the case for review.
3. `GET /bevilling/get_student_bevillinger/{cpr}` to collect existing `esdh_noegle` values for de-duplication.
4. Per bevilling: `POST /bevilling/create_bevilling/{cpr}`.
5. Per kørselsrække: `POST /bevilling/create_koerselsraekke/{bevilling_id}`.

Every call authenticates with `X-API-Key` from `API_KEY`.

### Mapping decisions worth knowing

| Source | Target | How |
|---|---|---|
| `SkoleID` | `matrikel_id` | `/lookup/skolematrikel` returns `skolekode` specifically so this bot can build the map without a second query |
| `HjemmelForBevilling` | `hjemmel_id` + `begrundelse_fra_formular` | `_HJEMMEL_MAPPING`, a hardcoded dict of the three values the legacy data actually contains |
| `Revurdering` | `revurderingsdato` | a **past** date is nulled out, so a converted bevilling is not immediately flagged for re-review |
| *(not used)* | `sagsbehandler_id` | hardcoded to `_SAGSBEHANDLER_NAVN` — see below. The source `Sagsbehandler` column is **discarded** |
| newest `Modified` in the bucket | `sagsbehandlingsdato` | when the bevilling was last worked on. The **newest** across the bucket's rows, not the first non-None — the rows were edited at different times. `ModifiedDate` is the fallback |
| `CaseID` | `esdh_noegle` | the PPR case id. The borgersag flow this bot once had was scrapped, so there is no separate ESDH case to resolve — the source case id is the reference. Also half of the de-duplication key |

## Environment variables (`.env`)

| Variable | Purpose |
|---|---|
| `ATS_URL`, `ATS_TOKEN` | Automation Server workqueue |
| `ATS_WORKQUEUE_OVERRIDE` | Override the workqueue id (dev/test) |
| `API_ENDPOINT` | Base URL of the befordring API |
| `API_KEY` | Sent as `X-API-Key`. Must match a hash in the target environment's `API_KEY_HASHES` — see that repo's `.env.example` |
| `DBCONNECTIONSTRINGSERVER29` | LOIS, read only. Same variable and same value as `rpa-befordring-nightly-runs`. **Optional** — used only to tell an unresolved address where CPR has that student living; unset, that one line is missing from the warning and nothing else changes |

The connection to the RPA database (`BefordringsData`) is **not** an env var; it is fetched at runtime from `RPAConnection`. `DBCONNECTIONSTRINGSERVER29` is separate and points at a different server.

## Known issues

Ordered by how much they matter at go-live.

1. **`TOP (10)`** is still in the query, marked as test mode. A real conversion would import ten cases.

2. **Weekdays are always `Alle`.** The source records none, so every converted kørselsrække claims every school day. Correct for a standing arrangement, wrong for the cases where two kørselstyper split the week between them — those need a caseworker.

3. **`main.py` still carries the SSL-bypass block** under the `🔥 REMOVE BEFORE DEPLOYMENT` banner. It is currently commented out; delete it rather than leave it to be uncommented by accident.


### How rows are bucketed into bevillinger

The legacy data cannot be converted one-bevilling-per-date-pair.
`usp_recalculate_bevilling_status` fails a citizen who ends up with more than
one **Aktiv** bevilling, and a student with two overlapping legacy rows would
produce exactly that — the whole case lands on Fejlet. So rows are grouped by
when they apply rather than by their exact dates:

| Bucket | Rule | Result |
|---|---|---|
| `current` | period overlaps `[today, window_end]` | **one** bevilling — computes as Aktiv |
| `future` | period starts after `window_end` | **one** bevilling — computes as Kommende |
| `past` | already ended | one bevilling **per distinct period** — Udløbet, which does not collide |
| `ukendt` | dates missing or unreadable | kept apart under the raw values, and logged |

`window_end` is one month from the run date, or `config.CONVERSION_WINDOW_END` when pinned. Pinning it makes the grouping reproducible across runs, which is worth doing once a conversion date is agreed.

Future rows are merged even where their periods are far apart. The legacy data is not detailed enough to split them into meaningful separate bevillinger, and only one may become Aktiv later in any case.

Nothing here assigns a status — the status engine derives Aktiv / Kommende / Udløbet from the kørselsrække dates once the rows exist. The bucketing only decides which rows share a bevilling.

Both grouping levels **sort before `groupby`**, which only groups consecutive equal keys. The query orders by `CaseID` alone, so the inner grouping was previously at the mercy of row order: a case whose rows ran date-pair A, B, A produced three bevillinger instead of two.

### Re-running a partial conversion

De-duplication keys on `(esdh_noegle, foerste_koersel_dato)`. Every bevilling converted from one case carries the same `esdh_noegle` — the PPR case id — so that alone could only answer "does this case have *any* bevilling?". A run that died after creating the first of three would, on retry, skip all three.

`foerste_koersel_dato` is the earliest `BevillingFra` in the bucket, written on create and read back from `view_Student_Bevillinger` (added to that view for this purpose). Newly created bevillinger are added to the in-memory set as they go, so two buckets cannot collide within a single run.

A bevilling is created *before* its kørselsrækker, so a run that stops between the two leaves one with none — sitting at Påbegyndt with nothing on it. Existence alone is therefore the wrong question: `_has_koerselsraekker()` asks whether it is actually finished, and a bevilling that is not gets **completed** rather than skipped or duplicated.

### Nothing is written until every lookup resolves

`create_bevilling()` resolves and validates **all** of a bevilling's kørselsrækker before it creates the bevilling.

It did not always. The lookups used to happen inside the POST loop, after the bevilling existed, so one unmappable value raised `BusinessError` with a bevilling already in the database — stranded at Påbegyndt with no kørselsrækker, and skipped as "already exists" on every retry afterwards. That is how the source's `"Egenbefordring"` spelling (see the normalisation section) left a case half-converted on a first clean run: the ATS retry loop re-processed the item, found the bevilling, and skipped it.

Validating first means an unmappable value fails the case with nothing written — the only safe order for a one-shot migration. The `_has_koerselsraekker()` check above still matters, but now only for genuine mid-flight failures such as a dropped connection.

**Known limit:** two buckets in one case can still share a start date — a short bevilling and a longer one beginning on the same day, where only one has ended. Those are indistinguishable afterwards. A clean run converts both correctly; only a *resumed* run would skip the second. The queue phase logs every such case by id, and those are the ones to check by hand if the conversion is ever restarted part-way.

### Fidelity: what the conversion can and cannot carry over

The old system records less than the new one does. A bevilling here needs a
rutetype, weekdays and kørselsrækker; `BefordringsData` has a date range, a
tidspunkt, a kørselstype and a distance. Some of the gap is bridgeable, some
is not, and the choices are:

| New field | Source | How |
|---|---|---|
| `rutetype_id` | `TidspunktForBevilling` | derived — see below |
| `dag_ids` | nothing | always `["Alle"]` |
| kørselsrækker | one per source row in the bucket | a student with a morning row and an afternoon row gets two rækker on one bevilling, which is the right shape |

`_RUTETYPE_FROM_TIDSPUNKT` derives the rutetype from the tidspunkt, because a
morning-only bevilling runs one way and an afternoon-only one the other:

| `TidspunktForBevilling` | `rutetype_tekst` |
|---|---|
| Morgen | Hjem til skole |
| Eftermiddag | Skole til hjem |
| Morgen og eftermiddag | Mellem hjem og skole |

Those three are exactly the seeded `Tidspunkt` values, so every row maps. The
targets are checked against the Rutetype lookup **at startup** rather than per
row: a renamed value would otherwise leave `rutetype_id` quietly unset on
every converted række.

Where the legacy data is clean this produces a good result — one row becomes
one kørselsrække with the right direction; a morning and an afternoon row
become two. Where it is not — three rows because two kørselstyper run on
different weekdays — the conversion keeps all three as separate rækker on the
same bevilling, with `Alle` weekdays on each. That is wrong in detail but
right in substance, and a caseworker narrowing the days later replaces `Alle`
rather than adding to it. Fixing it automatically would mean inventing
weekday splits the source never recorded.

### Lookup values are matched with whitespace removed

The two systems do not space these consistently:

```
BefordringsData   Egenbefordring
Befordringstype   Egen befordring
```

`_normalise()` casefolds and removes **all** whitespace before matching, rather
than aliasing that one value. Verified against every seeded lookup —
`Befordringstype`, `Tidspunkt`, `Rutetype`, `KoerselstypeTillaeg`, `Hjemmel`,
`Ugedag` — that removing spaces collapses no two values onto each other, so
nothing becomes ambiguous.

This is the third place in the system to need it. `view_Koerselsgodtgoerelse_Modtagere`
strips spaces before comparing, and so does `labelIsEgenbefordring` in the
frontend — both because of this same discrepancy, and
`recalculateEgenbefordringRows` silently never fired for months because it did
not.

Fuzzy matching that says nothing hides the problem it papers over, so
`_resolve()` logs when a source value matched only after normalising, naming
both spellings:

```
Kørselstype 'Egenbefordring' stored as 'Egen befordring' — matched after normalising.
```

The comparison is against the *stored* label, not the normalised key, so a
value both systems already agree on stays silent however many spaces it
contains.

### Kørselstype and tillæg arrive as one string

`BevillingAfKoerselstype` sometimes combines a kørselstype with a tillæg, where
the new system keeps them apart:

```
BefordringsData       Rutekørsel fast forsæde
Befordringstype       Rutekørsel
KoerselstypeTillaeg              Fast forsæde
```

`_split_koerselstype()` tries a direct match first, so a plain kørselstype can
never be mis-split. Only when that fails does it peel known tillæg off the
end, **longest first** — `Fast forsæde` and `Fast sæde` both exist, and taking
the shorter one first would leave `…for` stuck on the front of the type.

It loops rather than stripping once, so a value naming two tillæg resolves
without anyone adding a case for it. Anything that still will not resolve
after peeling raises `BusinessError`, exactly as an unknown kørselstype
always did.

A split is logged, naming what came out of it, since the source string no
longer appears anywhere in the result.

### Klub rows are flagged in the comment, not converted

Befordring to and from a klub does not exist in the old system but does here.
To record it anyway, caseworkers put the klub in `ElevensAdresse` and the
student's home in `SkoleNavnBefordring` — or the reverse, for the return trip.
The row therefore describes a journey its own columns misname, and nothing
automatic can recover which was which.

So the raw values are carried across verbatim, appended to that kørselsrække's
comment:

```
Fra foranstaltningsdata konvertering:
ElevensAdresse: Klubben Holme Søndergård, 8270 Højbjerg
SkoleNavnBefordring: Kærlundvej 16, 8260 Viby J
```

An existing comment is kept and the note added below it. Detection is per
**row**, not per bevilling — the marker sits on the individual row, and it is
that row's journey the note describes.

`_KLUB_MARKERS` holds the places to look for, currently just
`"Klubben Holme Søndergård"`. Matching goes through `_fold()`, which casefolds,
strips spaces and flattens æ/ø/å, so `"Søndergaard"` and `"Søndergård"` both
match without listing every spelling. Add a marker to the tuple as more turn
up.

The queue phase reports how many rows were flagged, so the size of the manual
follow-up is known before anything is converted. Note the bevilling itself
still converts with whatever address those columns produced — the note is what
tells a caseworker to rebuild the klub kørsel properly.

### Caseworker assignment

Every converted bevilling is assigned to one caseworker, `_SAGSBEHANDLER_NAVN`
(currently `"Sofie"`). The source `Sagsbehandler` column is discarded.

`BefordringsData` does name a caseworker, but those are legacy PPR staff and
do not line up with the `Sagsbehandler` table — which is not seeded reference
data but real people, created and retired as staff change. Matching on the old
names would leave most converted bevillinger with no caseworker at all, and
occasionally attach one to somebody who has left.

A single known owner is more useful: every converted bevilling is
identifiable and reassignable in one go afterwards.

Resolved once at startup and failing with a clear message if that name is not
in the table — `Sagsbehandler` is not populated by `seed_lookup_data.sql`, so
a fresh database has none until someone adds them.

**The legacy caseworker name is lost.** There is no free-text field on
Bevilling to park it in; `begrundelse_fra_formular` already carries the
hjemmel begrundelse. If it needs keeping, that is a schema question to settle
before the conversion runs, not after.

### Students are never created here

Every currently enrolled student is already in `Elev`: `rpa-befordring-nightly-runs` upserts the whole dump from `Elev_STG` long before this conversion runs. A CPR that is missing is therefore one the nightly load does not know — they have most likely left the municipality or finished school since the legacy bevilling was written.

This bot used to `POST /citizen/create_elev` with just `{cpr, adresse_id}`. That produced a row with no name, no `skolekode` and no `elevklassetrin`, which nothing would ever fill in: the nightly upsert only touches CPRs present in `Elev_STG`, and this one is not. It would also sit outside the school derivation permanently, because `usp_sync_elev_matrikel_from_bevilling` requires a non-zero `skolekode` before it will take a matrikel from the bevilling — and no school means no walking distance either.

So a miss now raises `BusinessError`, which sends the item to `pending_user` rather than failing it. Nothing the bot can do resolves it; a person has to decide whether that student should be converted at all.

### How addresses are matched

The two systems write the middle of an address differently, and the difference is **not one thing**:

```
supplementary place name — register only
  BefordringsData   Kærlundvej 16, 8260 Viby J
  Adresse           Kærlundvej 16, Ormslev, 8260 Viby J

floor and door — both, and load-bearing
  BefordringsData   Langkærvej 19, st. tv, 8381 Tilst
  Adresse           Langkærvej 19, st. tv, 8381 Tilst
                    Langkærvej 19, st. th, 8381 Tilst    <- different flat
                    Langkærvej 19, 1. tv, 8381 Tilst     <- different flat
```

Note also that `ElevensAdresse` **already includes the postcode and city** — `ElevensPostnummer` is a separate column holding `8381` again, and appending it produces nonsense. It is only used as a fallback when the address string has no postcode.

Classifying each middle part as "place name" or "floor/door" would mean encoding Danish address conventions and getting every variant right. `_matches()` sidesteps that with **subsequence matching**:

- street (first part) must be equal
- postcode (four digits of the last part) must be equal
- every middle part of the **source** must appear among the candidate's middle parts, in order

Extra parts in the candidate are therefore fine — that is the place name. Missing ones are not — that is a different flat. Everything is casefolded and whitespace-collapsed first.

A source with no floor/door still matches every flat at that number, which is correct: nothing in the data says which one, so several candidates survive and the address is refused rather than guessed.

`_search_prefixes()` decides what to search for, most selective first:

1. `"<street>, <first middle>,"` — e.g. `"langkærvej 19, st. tv,"`
2. `"<street>,"` — e.g. `"langkærvej 19,"`

The trailing comma is what makes a prefix safe: every `adresse_tekst` has one straight after the house number, so `"Kærlundvej 16,"` matches `"Kærlundvej 16, Ormslev, ..."` but not `"Kærlundvej 160, ..."` or `"Kærlundvej 16A, ..."`.

### The resolved-address cache

Address resolution is the slow part of the queue phase — one or more API calls per distinct address, over ~3700 rows of which nearly all resolve first time and never change. Re-running to inspect a handful of failures should not mean paying for the rest again.

`resolved_addresses.csv` (path in `config.RESOLVED_ADDRESS_CACHE`, `None` switches it off) holds one row per resolved address: the normalised key as JSON, the `adresse_id`, the register's spelling, and the source address for eyeballing. It is gitignored — a run artefact, not source.

- **Successes only.** A failure is never cached, so every re-run retries exactly the addresses still being worked on.
- **Written as it goes**, not at the end, so a run that dies half way keeps what it had.
- **The key is JSON**, not a joined string: an address component can contain almost any punctuation, and a separator appearing inside one would split it in the wrong place.
- Anything unreadable in the file is skipped rather than fatal. The worst a bad line costs is one address resolved again.

**Delete the file after changing the matching rules.** A cached hit skips the matcher completely, so an entry written under the old rules would survive the change meant to correct it.

### CPR settles an ambiguity

Several register rows fitting the source equally well often means the register holds both an access address and its unit address:

```
  Øster Kringelvej 25, 8250 Egå
      søgte på : 'øster kringelvej 25,' → 2 række(r), 2 match
      passer lige godt:
                 Øster Kringelvej 25, 8250 Egå
                 Øster Kringelvej 25, st., 8250 Egå
```

That is the same dwelling written twice, and no rule about the source text can separate them — the source says `25` and both rows are `25`. CPR can: it says which row the student is registered at. Where exactly one candidate is the student's own address, that one is taken, and the choice is logged by name.

Deliberately narrow, in two ways:

- **Only an ambiguity, never a miss.** Choosing among candidates that already matched keeps the answer consistent with the source. Where *nothing* matched, CPR's address is not among the candidates, and taking it would invent an address the source never supported — `Hørret Byvej 15` with CPR saying `15A` stays unresolved, because that is precisely the case a caseworker must look at.
- **Only on agreement.** Several students can share one legacy address. If their CPR addresses point at different candidates, that is a new disagreement rather than an answer, and it stays unresolved with both shown.

### Reading an unresolved address

Each failure logs the source verbatim, every search that ran with what it returned, and — from one extra probe made only on failure — what the register actually holds on that street. The probe runs **only when nothing matched**, and it tries every street spelling the matcher itself tried — not just the source's raw wording. Probing the raw wording alone is how `Borresøvej 041` once reported the neighbours of `Borresøvej 10`: the padded street found nothing, so it fell straight through to the bare street name, where alphabetical order starts at 10, while `borresøvej 41` would have found the building. The bare street name is kept as a last resort, to answer "does this street exist at all".

The probe drops the trailing comma the matcher's own searches all end in: that comma is what stops `Hørret Byvej 15,` matching `Hørret Byvej 150,`, but it also means that when the register has `15A` and no bare `15`, every search returns nothing and the log can only say `0`.

A failure also reports **where CPR has that student living**, looked up in `LOIS.CPR.PersonGeoView` on `PNR_0` — the same view and the same key `rpa-befordring-nightly-runs` uses to resolve `Elev.adresse_id` every night — and turned into text through `GET /adresse/{adresse_id}`. For `Hørret Byvej 15` that is `15C`, which is the correction a caseworker would otherwise have looked up by hand, one student at a time.

It is a hint, not an answer: a bevilling is granted at the address it was granted at, so a student who has since moved *should* differ. But it is the one other fact about this student's address that exists.

One batched query covering every failure, and only on failure — a clean run never touches LOIS. Every error is swallowed and logged as a note: it is not certain this connection reaches LOIS at all, since `BefordringsData` is in `[RPA]` and the view is in `[LOIS]`. If they are not on the same server the lookup simply says so and the conversion carries on.

Three signatures, and they say different things:

| in the log | cause |
|---|---|
| `0 række(r)` on every search, then `registret har:` | **source data** — the address does not exist as written; the caseworker left something off |
| `N række(r), N match`, then `passer lige godt:` | **ambiguous** — the address was found; the source just does not say which flat. The list is the candidates themselves, not a fresh probe of the street |
| every search `0` *and* `registret har: intet`, usually with `tegn:` | **the string is broken** — a hidden character, or a street that is not there at all |

```
  Hørret Byvej 15, 8320 Mårslet
      normaliseret : hørret byvej 15 | 8320 mårslet
      søgte på     : 'hørret byvej 15,' → 0 række(r), 0 match
      registret har:
                     Hørret Byvej 15A, 8320 Mårslet
                     Hørret Byvej 15C, 8320 Mårslet
                     Hørret Byvej 15D, 8320 Mårslet
      => adressen findes ikke som skrevet; se ovenstående
```

`tegn:` is a repr, added only when the source holds a character a Danish address is not written with — a zero-width space, a soft hyphen or a decomposed `å` breaks matching while looking perfectly normal on screen.

### The search must be narrowed to the postcode

`Adresse` is the whole of Denmark — the nightly import reads `LOIS.DAR.AdresseDkGeoView` with no municipality filter, ~4M rows. `/adresse/search` orders alphabetically, and on a string whose next characters are the postcode that means the rows falling off the end of the limit are the ones with the **highest** postcodes. Searching `"Bøgebakken 2,"` returns Greve, Roskilde and Køge, and `8462 Harlev J` is never seen.

So every search sends `postnummer` (from the source address itself) and `limit=200`. The postcode filter is a contains match, but it runs on top of the prefix index seek, so it only touches rows already narrowed to. The raised limit covers the other truncation cause — a single block of flats can exceed 15 on the street-only prefix, and a truncated result looks like an *absent* address rather than an ambiguous one.

A human in the combobox notices a missing address and types more. A robot records "no match" and moves on, which is why this was invisible until the addresses were listed side by side.

### Initials in street names

| source | register |
|---|---|
| `M. P. Hansens vej 14` | `M.P. Hansens Vej 14` |

`_canon` already makes these compare equal — it removes spaces and periods, so both become `mphansensvej14`. The search is what fails: `'m. p. hansens vej 14,'` is not a prefix of `M.P. Hansens Vej 14,`, so nothing comes back for the comparison to work on. Both spellings are therefore generated as search variants.

The rewrite fires only where a single letter and a period are followed by **another** single letter and period. That lookahead is what keeps it off ordinary words: `M. P. Hansens` is rewritten, `P. Hansens` and `Skt. Clemens` are left exactly as they are. Without it, `m. p. hansens vej` would collapse to `m.p.hansens vej`, which matches nothing — and `Skt. Clemens Torv` would break an address that currently works.

### Zero-padded numbers

The source pads both house numbers and floors, and the register pads neither:

| source | register |
|---|---|
| `Torstilgårdsvej 34, 02 tv` | `Torstilgårdsvej 34, 2. tv` |
| `Borresøvej 041` | `Borresøvej 41` |

DAR's canonical husnummer is 1–3 digits with no leading zeros, so a padded number can only ever mean the unpadded one — there is no distinct address to confuse it with, and `70` is untouched because it has no leading zero.

The two halves are not equally dangerous. A padded **floor** is recoverable: the street-only prefix still finds the building and the comparison sorts it out. A padded **house number** is not — the padding sits in the street component, so every prefix built from it is wrong, including the fallback. It has to be spelled correctly or the address is lost outright.

Two guards on the rule:

- **Runs of at most three digits.** A four-digit run is postcode-shaped, and Denmark does have postcodes beginning with zero — `0800`, and the `0900`–`0999` København C range. `_canon` is not applied to the postcode component either, so that is two independent guards.
- **Stripped before the spaces are collapsed.** Once `"vestergade 041"` has become `"vestergade041"` the zero is no longer recognisable as leading, and the rule would either do nothing or hit the wrong digits.

### The source sometimes mis-punctuates the postcode

```
Rosenhøj Bakke 20, 3. tv.  8260 Viby J
                        ^ a period where a comma belongs
```

Splitting on commas alone leaves `3. tv. 8260 viby j` as one part, which then has to turn up among the register's middle parts — it never will — and with no postcode at its head `_address_key` drops the address outright. A four-digit postcode followed by a town is unmistakable wherever it sits, so `_components` splits the last part there and trims the stray period. Only the last part, and only when it does not already begin with a postcode, so nothing correctly written is touched.

### Punctuation differs between the two systems

The same address is punctuated differently on each side, and the differences are purely typographic:

| | BefordringsData | Adresse |
|---|---|---|
| floor and door | `Drosbjerg 16, 1 th` | `Drosbjerg 16, 1. th` |
| house-number letter | `Egå Mosevej 31 c` | `Egå Mosevej 31C` |

`_canon` collapses both pairs onto one string by dropping every period and every space (`1th`, `31c`). It is deliberately blunt — classifying the parts properly would mean parsing Danish address conventions, and this only has to decide whether two spellings name the same place. Blunt is safe because ambiguity is already refused: an address is accepted only when exactly **one** candidate matches, so two register rows that canonicalise alike go to manual follow-up rather than being guessed between. It is not applied to the postcode component, where `_postcode_of` needs the word boundary after the four digits.

`_canon` fixes comparison, but the *search* is a prefix `LIKE` against `adresse_tekst`, so a prefix still has to be spelled the register's way. `_street_variants` and `_floor_variants` generate both spellings of each — with and without the space in a house-number letter, with and without the floor's period — and `_search_prefixes` tries them most-selective first, stopping at the first that matches. A correctly spelled address therefore still costs one request; only the awkward ones escalate.

Every queue item logs its addresses as it is built — each source address as BefordringsData wrote it, and the register address it matched, or `— INTET MATCH —`:

```
Kø-emne 1 | PPR-sag PPR-1 | CPR 0101011234 | 1 bevilling(er)
    [current]
      kilde: Klubben Holme Søndergård: Nygårdsvej 5, 8270 Højbjerg
      match: — INTET MATCH —
      kilde: Drosbjerg 16,1 th, 8260 Viby J
      match: Drosbjerg 16, 1. th, 8260 Viby J
```

Both sides are printed because the two systems punctuate the same address differently: a match that looks wrong at a glance usually is not, and one that genuinely is wrong is only visible with the source beside it. Per item rather than as one table at the end, so it reads in the order the queue was built.

An unresolved address logs the searches that came back empty, so the prefix can be pasted straight into the address field to see what the register actually has. Where the text contains a character a Danish address is not written with, it also logs a repr — a zero-width space, a soft hyphen, an en-dash or a decomposed `å` breaks matching while looking perfectly normal on screen, and `0 candidate(s)` on its own is not diagnosable:

```
  rosen​høj bakke 20, 3. tv, 8260 viby j — 0 candidate(s)
      søgte på: rosen​høj bakke 20, 3. tv, | rosen​høj bakke 20, 3 tv, | rosen​høj bakke 20,
      tegn:     'rosen\u200bh\xf8j bakke 20, 3. tv, 8260 viby j'
```

The floor variants exist purely for the 15-row cap. The street-only fallback would find these buildings anyway, but a block with more than 15 flats pushes the wanted row out of the results, where it looks absent rather than ambiguous.

Including the floor matters because `/adresse/search` caps at **15 rows**. A block of flats exceeds that on the street prefix alone, and the wanted address would be pushed out of the results and look absent. The street-only prefix is kept as a fallback for the case where the register puts a place name where this assumes the floor is.

Matching relies on the database collation being case-insensitive for the `LIKE` — standard for this instance, but it is a dependency.

A candidate is accepted only when **exactly one** survives. Placing a bevilling at the wrong address is worse than failing to place it.

The first `--queue` run logs the resolution rate and lists every unresolved address with its candidate count. `0 candidate(s)` means absent or spelled differently; `2+` means genuinely ambiguous — usually a flat with no floor/door in the legacy data — and needs a human.

## Verified compatible

Checked against the befordring backend as of migration 022. These are fine and need no change:

- `ansoegningstype` now sends `"Fast kørsel"` (was `"Kørsel"`, retired by migration 009). Legacy PPR bevillinger are all standing arrangements, so that is the correct half of the split.
- `KoerselsraekkeCreateRequest` requires only `gyldig_fra`, `gyldig_til`, `tidspunkt_id`, `befordringstype_id`; all four are sent, plus `rutetype_id` and `dag_ids: ["Alle"]` — so a converted række satisfies the application's own form and can be edited without first filling gaps.
- `foerste_koersel_dato` is accepted by `BevillingCreateRequest` and, since this change, returned by `view_Student_Bevillinger` — **that view must be redeployed** or the de-duplication silently never matches.
- `begrundelse_fra_formular` became nullable in migration 017. Before that, a bevilling whose hjemmel was not in `_HJEMMEL_MAPPING` would have been rejected, because the bot strips `None` from the payload.
- Both hjemmel targets — `§ 26, stk. 1 afstand` and `§ 26, stk. 2 sygdom` — exist verbatim in `seed_lookup_data.sql`.
- The `sfo` → `institution` rename (migration 013) does not affect this bot: it sends neither field.
