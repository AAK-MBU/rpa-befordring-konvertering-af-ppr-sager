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

### Schools split across two sites

Five schools run on two sites under **one skolekode**:

```
Stensagerskolen (Janesvej)          751903
Stensagerskolen (Stensagervej)      751903
Kaløvigskolen (Sanatorievej)        751020
Kaløvigskolen (Skovager)            751020
Langagerskolen (Bøgeskov Høvej)     751090
Langagerskolen (Kolt Østervej)      751090
Tranbjergskolen (Grønløkke Allé)    280458
Tranbjergskolen (Kirketorvet)       280458
Vestergårdsskolen (Nordbyvej)       751050
Vestergårdsskolen (Stensagervej)    751050
```

`SkoleID` alone therefore does **not** identify a matrikel. `_vaelg_matrikel` tells the sites apart on the street in `SkolensAdresse`, matched against the site name the lookup label carries in parentheses — `Stensagervej 11` against `Stensagerskolen (Stensagervej)`. Compared through `_uden_accent`: case and spacing ignored, accents folded, and `æ/ø/å` flattened to `ae/oe/aa`. All three are needed — the source writes `Grønløkke Alle` for the seeded `Grønløkke Allé`, and `Groenloekke` as readily as `Grønløkke`. Verified that no pair of sites sharing a skolekode collapses onto the other under that fold. `SkoleNavnBefordring` is tried too, because the source sometimes names the site there instead: `Stensagerskolen (afd. Stensagervej)`.

**Every row's pair is tried, not just the bevilling's first non-None.** `SkolensAdresse` and `SkoleNavnBefordring` are bevilling-level columns, so where the first row is a klub row they name the *klub* — `Nygårdsvej 5, 8270 Højbjerg` — and the site is unfindable even though a sibling row states it plainly. `_skole_kandidater` collects every distinct pair in the bevilling and passes them all.

The rows must **agree**. One matrikel across all of them is the answer; two different ones is a real disagreement — some rows to Janesvej, some to Stensagervej — and that is a case for a human, not a coin toss. The error names both the pairs tried and the sites they pointed at.

An unknown skolekode still yields no matrikel, exactly as before. A known one whose site cannot be settled **raises** and the case goes to `pending_user` — a wrong school is worse than a stopped case.

This was a live bug: `skolematrikel_map` was a dict keyed on skolekode, so the last entry won, and the lookup is ordered by `matrikel_navn`. Every `751903` student was being given `Stensagervej`, including the ones at `Janesvej`.

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

### Hjælpemidler and tillæg named in a comment

Neither can be set reliably during the conversion. Hjælpemidler have **no source field at all**, and a tillæg is only recoverable where it was written into `BevillingAfKoerselstype` — a mention in free text carries no structure to convert.

So the mention is surfaced rather than guessed at. `_hjaelpemiddel_traef` scans the source `Kommentar` for the lookup values plus the stem `hjælpemid`, and the kørselsrække gets:

```
KONVERTERING-PPR | hjælpemiddel eller tillæg nævnt i kommentaren
Fundet: El-kørestol, Fast forsæde
Kommentar: Barnet sidder i el-kørestol og skal have fast forsæde
Hjælpemidler og tillæg kan ikke sættes pålideligt ud fra fritekst, så de er
IKKE sat på bevillingen. Kontrollér og sæt dem manuelt.
```

- Matched through `_fold`, so `hjælpemiddel`, `hjælpemidler`, `HJAELPEMIDLER` and `el-koerestol` all hit. Only the **stem** is listed, not every ending.
- A term wholly contained in another match is dropped, so `el-kørestol` reports once rather than as both `El-kørestol` and `Kørestol`.
- Read from the **original** `Kommentar`, not the one being built — otherwise a note this run added would be matched back at us.

**`_HJAELPEMIDDEL_ORD` must be kept in step with the `Hjaelpemiddel` and `KoerselstypeTillaeg` lookups** in `backend/db/seed/seed_lookup_data.sql`. A value added there and not here is simply never flagged; nothing breaks, the mention is just missed.

### Klub rows

The old system had no klub kørsel. To record it anyway, caseworkers wrote the klub into whichever field was to hand — `ElevensAdresse`, with the home in `SkoleNavnBefordring`, or the reverse for the return trip, or simply a line in `Kommentar`. The row therefore describes a journey its own columns misname, and nothing automatic can recover which was which.

**Detection** is across all three fields (`_KLUB_FELTER`), matched through `_fold` so case, spacing and `æ/ø/å` versus `ae/oe/aa` all hit. Markers are `klub` — which catches *Klubben*, *klubben*, *ungdomsklub* — and the phrase `Holme Søndergård`.

Bare `holme` is **deliberately not** a marker. Holmevej, Holme Ringvej and Holmesvinget are ordinary Aarhus streets, and matching them would drag every student living in Holme onto this path. Over-matching on `klub` costs only a comment; over-matching on `holme` would move real addresses.

**The bevilling goes on the student's address, never the klub's:**

1. A row whose `ElevensAdresse` is the klub supplies **no** address candidate. Its siblings — the return trip, where the home sits in `ElevensAdresse` — still do.
2. If that leaves nothing, the student's current address from LOIS is used.
3. Failing both, the bevilling is rejected as usual.

Note that `_klub_i_adressen` and `_klub_relevant` are different tests on purpose. A klub named in `SkoleNavnBefordring` or `Kommentar` does not disqualify that row's address — only a klub sitting in the address field does.

**Every kørselsrække in the bevilling** then carries a comment, not just the rows that mention the klub, because it is the bevilling as a whole that needs rebuilding. Each carries its **own** row's raw values, so a caseworker can see which journey said what:

```
KONVERTERING-PPR | klub kan indgå i befordringen
Kilde (denne kørselsrække):
  ElevensAdresse: Klubben Holme Søndergård: Nygårdsvej 5, 8270 Højbjerg
  SkoleNavnBefordring: Møllevangskolen
  Kommentar: (tom)
Bevillingen er oprettet på elevens egen adresse: Kærlundvej 16, Ormslev, 8260 Viby J
Det gamle system havde ikke klubkørsel, så klubben er skrevet ind i felterne
ovenfor. Ret bevillingen manuelt, så den afspejler den faktiske befordring.
```

The note is written after the address is settled, because it names the address the bevilling ended up on.

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

### Every conversion comment is marked

Each comment this bot writes onto a kørselsrække begins with the same string, and nothing else in the application writes it:

```
KONVERTERING-PPR | <what kind of note this is>
<the detail>
```

So the full list of converted bevillinger needing a look is one query:

```sql
SELECT DISTINCT b.bevilling_id, b.cpr_elev, b.esdh_noegle, k.koersel_id, k.kommentar
FROM   befordring.Koersel   k
JOIN   befordring.Bevilling b ON b.bevilling_id = k.bevilling_id
WHERE  k.kommentar LIKE '%KONVERTERING-PPR%'
ORDER  BY b.cpr_elev, b.bevilling_id;
```

**No square brackets, percent signs or underscores in the marker, on purpose.** All three are metacharacters in T-SQL `LIKE`, and a marker containing them would silently match far more than intended — `LIKE '%[KONVERTERING]%'` matches any single one of those letters.

The kinds, which is the text after the pipe:

| `emne` | written when |
|---|---|
| `adressematch` | resolved to one row, but not word for word |
| `adresse valgt via CPR` | several rows fitted; CPR chose |
| `adresse rettet via CPR` | the source's floor/door matched nothing; CPR named the row |
| `adresse antaget — manglende etage/dør` | several rows at one coordinate; the flat is a guess |
| `lukket sag — elevens nuværende adresse` | closed case, address unresolvable, student's own address used |
| `klub i kildedata` | the row named a klub in `ElevensAdresse` or `SkoleNavnBefordring` |

Every builder goes through `_konverterings_note`, so the marker cannot be left off a new one. A kørselsrække can carry more than one — a klub row whose address also needed normalising gets both — and any caseworker text already on the row is kept above them.

### Resolution order

An address is resolved by the first of these that answers. Each step is weaker than the one above it, and everything below the first is recorded on the kørselsrække:

| # | step | evidence |
|---|---|---|
| 1 | exactly one register row matches | the source itself |
| 2 | several matched, CPR names one of them | CPR |
| 3 | none matched, but the source **is** the student's registered address once separators are dropped | CPR, no search involved |
| 4 | none matched, CPR names a row at the same street and house number | CPR |
| 5 | none matched, and the register holds exactly **one** address at that street and house number | uniqueness |
| 6 | several matched and all sit at the same coordinate | position only |
| 7 | nothing matched, but every address at that street and number is one point | position only |
| 8 | the case is closed and LOIS knows the student | the student's current address |
| — | otherwise | rejected for manual follow-up |

**Every address whose match required an inference carries a comment on its kørselsrækker**, saying what was read into the source and asking a caseworker to check. Steps 2–5 always do. Step 1 does only when `_er_i_praksis_samme` says the two texts are not simply the same address written differently.

Two things are **not** inference, and get no comment:

| | source | register |
|---|---|---|
| punctuation, spacing and commas | `Haurumsvej 13.1.th, 8381 Tilst` | `Haurumsvej 13, 1. th, 8381 Tilst` |
| parts only the register has | `Østervang 25, 8380 Trige` | `Østervang 25, Spørring, 8380 Trige` |

Neither says anything was worked out. Commas count as punctuation here: once whitespace, commas and periods are all removed, `Haurumsvej 13.1.th` and `Haurumsvej 13, 1. th` are one string, so splitting the floor off the street told us nothing that could have been wrong.

Everything else is. A stripped leading zero (`Borresøvej 041` → `41`), a merged floor and door (`143,1,-2` → `143, 1. 2`), an expanded abbreviation (`I. Christensens Gade`), a street split off from a floor (`Sjællandsgade 95A 1. sal`) — each is a *reading* of the source that could be wrong.

The test runs against the **raw** source, before phrase corrections and address overrides, so those always show up as inference.

The comment names the method, so the cheap checks stay distinguishable from the real ones: a normalised match reads differently from a coordinate guess, which reads differently again from a closed case placed on the student's current address.

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

### Reversed kørsel dates

The API rejects `gyldig_fra` after `gyldig_til` outright, so such a row cannot be created.

**Checked before the bevilling is written**, alongside the lookups. It used to surface as a 400 during the POST loop, which left a bevilling created with no kørselsrækker — and the retry then adopted that bevilling and failed on it again, every time. Exactly the failure the pre-create pass exists to prevent; date ranges had simply been left out of it.

**The dates are swapped, on every case.** Transposition is the overwhelmingly likely explanation, swapping yields a plausible period and keeps both original values, and refusing would lose the row — on an open case just as much as a closed one.

Only the comment differs, because the follow-up does:

```
KONVERTERING-PPR | lukket sag — byttede datoer          <- closed
...
PPR-sagen er lukket og kan ikke rettes, og rækken ville ellers ikke kunne
oprettes. Kontrollér perioden, hvis den får betydning.

KONVERTERING-PPR | byttede datoer                        <- open
...
Rækken kunne ellers ikke oprettes. Sagen er ÅBEN — ret datoerne i
foranstaltningsdata, hvis perioden ikke er rigtig.
```

The run log counts the two separately and lists the open case ids, since those are the ones someone can still fix at source.

The check in `bevilling_creation` stays as a **safety net**: an item queued before the swap existed could still arrive inverted, and failing before the write beats a 400 halfway through. `bevilling_creation` itself still knows nothing about the closed-case list — all data repair happens at queue time, and creation only validates and posts.

### A bevilling that borrows an address

`process_item` rejects the **whole case** when any bevilling lacks an `adresse_id`. So one unresolvable historic address costs the student their active bevilling as well — data that never arrives, for the sake of a period that ended years ago.

Where a bevilling resolves nothing of its own, it borrows from **another bevilling on the same case** — and only from there, and only when they all agree on one address. Two different ones means the student moved, and picking between them is guessing which era this bevilling belongs to.

The student's current address from CPR is deliberately **not** a fallback here. Where BefordringsData carries no usable address anywhere on the case, there is nothing to convert from, and placing the bevilling wherever the student lives today would invent a fact the source never stated. Those cases are rejected and land on the worklist.

That is what separates this from the klub and closed-case fallbacks, which *do* use CPR: there the source says something, it just cannot be used. Here it says nothing at all.

The borrowed address leaves a comment on all the bevilling's kørselsrækker:

```
KONVERTERING-PPR | adresse lånt fra sagen
Kilde: Ellemosevaenget 43 gl. adr, 8310 Tranbjerg J
Bevillingens egen adresse kunne ikke slås op i adresseregistret.
Brugt i stedet: Ellemosevænget 43, 8310 Tranbjerg J (samme sags øvrige bevillinger).
Uden en adresse kunne bevillingen slet ikke oprettes, og hele sagen ville
være afvist. Kontrollér adressen.
```



### Closed PPR cases

`BefordringsData` does not say whether a case is still open, so the list is exported from ESDH by hand into `Lukkede foranstaltningsmapper.csv` — `Sags ID` matching `CaseID`, and `Status`, where only rows saying `Lukket` count. Path in `config.CLOSED_CASES_CSV`. Gitignored; read with `utf-8-sig`, since an export opened in Excel carries a BOM that would otherwise make `Sags ID` unfindable.

Where a **closed** case has an address that will not resolve, the bevilling is created on the student's current address from LOIS instead of being rejected, and every kørselsrække says so:

```
Adresse fra foranstaltningsdata konvertering:
Kilde: Findes Ikke Vej 99, 9999 Ingensteds
Adressen kunne ikke findes i adresseregistret, og PPR-sagen er lukket og kan
derfor ikke rettes.
Bevillingen er i stedet oprettet på elevens nuværende adresse: Nyvej 7, 8000 Aarhus C
```

The reasoning is that a closed case cannot be edited, so nobody can ever correct the address — and its bevilling is not active, so an imprecise address costs nothing, while dropping the row loses data that cannot be recovered.

Narrow on purpose:

- **Only when no row in the bevilling resolved.** One that did gives a real address, which always beats a substitute.
- **Only for cases in the file.** An open case with the same broken address is still rejected, because there a caseworker can and should fix it.
- **Only when LOIS knows the student.** No current address means no substitute, and the case is rejected as before.

A missing file is normal: no case is treated as closed and everything fails exactly as it did.

### A floor and door that match nothing, corrected by CPR

```
source     Steen Billes Gade 8, 3. tv, 8200 Aarhus N
register   Steen Billes Gade 8, 3., 8200 Aarhus N     <- no door at all
CPR        Steen Billes Gade 8, 3., 8200 Aarhus N
```

The building and the floor both exist; the source has invented a door the register does not use. No rule about the text can bridge that — a missing part is exactly what `_matches` must refuse, or every wrong flat would match every other one. CPR names the row outright.

The pool CPR may pick from is every row the searches **returned**, matched or not. That is what separates this from inventing an address: all of those rows begin with the source's own street and house number, because the search prefix ends in a comma. The answer is therefore always the building the source named, and only the floor and door — the part the source got wrong — come from CPR.

Which is exactly why `Hørret Byvej 15` with CPR saying `15A` is still refused: `15A` does not start with `Hørret Byvej 15,`, so it was never in the pool. A different house number is a different address, and a caseworker has to make that call.

The kørselsrække records it:

```
Adresse fra foranstaltningsdata konvertering:
Kilde: Steen Billes Gade 8, 3. tv. 8200 Aarhus N
Adressen findes ikke som skrevet i adresseregistret — etage/dør passer ikke.
Eleven er iflg. CPR registreret på: Steen Billes Gade 8, 3., 8200 Aarhus N
Samme vej og husnummer, så bevillingen er oprettet der.
```

### A floor on an address that has none

```
source     Poul M. Møllers Vej 33, st, 8000 Aarhus C
register   Poul Martin Møllers Vej 33, 8000 Aarhus C     <- no floor at all
```

The source names a floor the register does not use, because there is only one dwelling at the number and nothing to distinguish. `_matches` must refuse that — a source middle with no counterpart is exactly how a wrong flat would otherwise match — but when the register holds **exactly one** row at the street and house number, there is no other dwelling it could be.

Uniqueness is the entire safety. A block of flats returns several rows, none matching a floor the source got wrong, and this refuses: `Blokvej 4, 3. mf` against a register holding only `1. th`, `1. tv` and `2. th` stays unresolved. Only a single-dwelling address gets through.

Street and postcode must still be equal. The rows come from prefixes built on the source's own street, so that is nearly given, but a street variant could have reached a neighbour and the check makes it explicit.

Always commented — the source said something the register does not confirm.

### Nothing matched, but the building is one point

```
source     Sifsgade 39, 2. 8230 Åbyhøj      floor 2, no door
register   Sifsgade 39, 2. 1 … 2. 6         six flats on that floor
           plus 27 more, every one at 56.1502667 / 10.1693075
```

A source middle with no counterpart is fatal, and rightly so. But the register gives the whole building **one coordinate**, and the coordinate is what the application uses for walking distance and routing. Refusing converts nothing; taking one gives the right position and a flat that needs correcting.

**Narrowed to the floor the source did name** before choosing: every source middle must begin one of the candidate's. The source said `2`, so the six rows whose middles start with `2` are preferred over the twenty-seven that do not — `Sifsgade 39, 2. 1` rather than `1. 1`. A guess on the right floor beats a guess on any floor.

Guards: a true miss only, street and postcode equal, and every row in the set being chosen from at the **same non-null point**. `Spredtvej 4` whose two flats sit at different coordinates is still refused — different points are different places.

This is the weakest step in the chain and runs last. Always commented, and the comment says the dwelling is a guess:

```
KONVERTERING-PPR | adresse antaget ud fra vej og postnummer
Kilde: Sifsgade 39, 2. 8230 Åbyhøj
Kildens etage/dør passer ikke på nogen af de 33 boliger, registret har på
vejen og husnummeret — men de ligger alle samme sted.
Valgt: Sifsgade 39, 2. 1, 8230 Åbyhøj
Placeringen er derfor rigtig, men boligen er et gæt og skal rettes manuelt.
```

### A missing floor, resolved by coordinates

Where CPR cannot settle an ambiguity, one case remains that is worth converting anyway. A source address with no floor or door matches every flat in the block — and the register gives all of them **the same latitude and longitude**, one point for the building:

```
Trige Parkvej 15, 1. mf, 8380 Trige    56.2511085  10.1529227
Trige Parkvej 15, 1. th, 8380 Trige    56.2511085  10.1529227
Trige Parkvej 15, st. tv, 8380 Trige   56.2511085  10.1529227
```

The coordinate is what the application actually uses — walking distance to school, and routing — so any of them carries the same consequences. The first by address text is taken (the search returns them ordered, so the choice is stable across re-runs) and the kørselsrække gets a comment:

```
Adresse fra foranstaltningsdata konvertering:
Kilde: Trige Parkvej 15, 8380 Trige
Adressen mangler etage/dør og passede på 9 boliger i adresseregistret,
som alle har samme placering.
Valgt: Trige Parkvej 15, 1. mf, 8380 Trige
Placeringen er dermed korrekt, men boligen skal rettes manuelt.
```

This is an **assumption, not an answer** — the dwelling is probably wrong and a caseworker must correct it, which is exactly what the comment says. It is therefore tried only after CPR, which names the actual dwelling and needs no warning.

It refuses whenever the coordinates differ or any is missing. Different points mean genuinely different places, and guessing between those would put a bevilling somewhere the source never pointed.

The `note` column in `resolved_addresses.csv` carries the comment, so a cached hit does not silently drop the warning.

### `helpers/tjek_elever.py` — are the students known to us?

```
python -m helpers.tjek_elever              # every distinct CPR
python -m helpers.tjek_elever --limit 50   # a quick sample
```

Standalone, not part of the conversion. Writes nothing anywhere and calls no mutating endpoint. Run it **before** a conversion so the gaps are known up front rather than discovered one failed case at a time.

It reads BefordringsData over **the same two-year window the conversion does** (`BevillingFra` and `BevillingTil` within the last two years). The two queries must be changed together — a report covering rows the conversion never reads would list students nobody is going to convert.

For every distinct CPR in `BefordringsData` it answers two independent questions:

| | source | what a miss means |
|---|---|---|
| **in Elev?** | `befordring.Elev`, read directly | every bevilling for that student is rejected — `cpr_elev` is a trusted FK, so the row cannot be created |
| **in LOIS?** | `LOIS.CPR.PersonGeoView` | no fallback address, so a klub row or closed case with an unresolvable address has nothing to fall back on |

`Status_T` from that view is reported alongside — CPR's own status text, carried through **verbatim rather than interpreted**, since the vocabulary is CPR's and not ours. It is often what explains a row that looks wrong for no visible reason. The console prints every value that turned up with a count, so the spread is visible before anyone decides what any of them should mean.

Note that `vurdering` does **not** take it into account: a student marked as having moved abroad still reads `ok` if they are in both sources. Once the real values are known, the meaningful ones are worth folding in.

They fail differently, so they are reported separately rather than as one "known" flag. `vurdering` combines them into one line, and the report is sorted worst-first.

Deliberate differences from the conversion's own LOIS lookup:

- **No `AdresseId IS NOT NULL` filter.** A person CPR knows but has no resolved address for is a distinct and useful case, and the report has a column for it.
- **A failed API call records `?`, not "no".** A network blip must not read as "this student does not exist".

**Both checks are batched.** With `DBCONNECTIONSTRINGBEFORDRING` set — the same variable the nightly run uses — the Elev check is a few chunked `IN (…)` queries rather than one HTTP call per student: 2000 CPRs cost three queries instead of two thousand round trips.

Direct SQL is a deliberate exception here. The conversion itself is API-only on purpose; a read-only diagnostic that already queries LOIS directly has no such constraint, and the API has no bulk endpoint to use instead.

Without that variable it falls back to `/citizen/stamdata/{cpr}`, one request per CPR in a small thread pool (`--workers`, default 8), and says so. `--api` forces the fallback even when the connection is available — useful for checking the API and the database agree.

Output is `elevtjek.csv` and `elevtjek.xlsx`.

### The unresolved worklist (`uloeste_adresser.csv`)

Every address the run could not resolve, written out for the caseworkers. Path in `config.UNRESOLVED_ADDRESS_CSV`, `None` switches it off, gitignored.

| column | |
|---|---|
| `ppr_sag` | the PPR case to act on |
| `cpr` | the student |
| `kilde_adresse` | the address exactly as BefordringsData wrote it |
| `elev_adresse_iflg_cpr` | where CPR has that student living — usually the correction |
| `aarsag` | `intet match` / `flertydig — N boliger` / `klubadresse (forventet)` |
| `registret_har` | the addresses the register does hold nearby |

**One row per case and student behind each address**, because that is the grain someone acts on — an address shared by two cases needs looking at twice. Sorted by case.

**Overwritten every run**, on purpose: it is a snapshot of *this* run's failures, so an address fixed at source disappears from it rather than lingering.

`aarsag` is the column to filter on first. A **klubadresse** is expected not to resolve and its bevilling is created on the student's own address — it is in the file only so the file and the log agree, not because it is work.

Written **twice**, as CSV and as `uloeste_adresser.xlsx` — same rows, same order. The CSV is `utf-8-sig` so Excel opens `æ/ø/å` correctly; the spreadsheet adds what a CSV cannot carry:

- a **filterable header**, so `aarsag` narrows to the rows that are actually work in one click
- a frozen top row
- column widths wide enough to read an address without dragging, wrapped and top-aligned

`openpyxl` is imported inside the writer rather than at module load, so a missing package costs the spreadsheet and nothing else — the CSV is already written by then, and the conversion depends on neither. It is in `pyproject.toml`, but an environment that has not been updated logs a line and carries on.

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

### Manual phrase corrections (`adresse_erstatninger.csv`)

For wordings no rule can derive. The case it was built for is an abbreviated street name:

| Find | Erstat |
|---|---|
| `I. Christensens Gade` | `Inger Christensens Gade` |

Nothing about the text says what `I.` stands for, and no register lookup can find out — so it is simply written down. Two columns, `Find` and `Erstat`; add a row and re-run. No code change, nothing else to touch.

- Matched **case-insensitively** and **across any amount of whitespace**, so `I.  Christensens  Gade` and `i. christensens gade` hit the same row.
- **Longest phrase first**, so a specific correction is not pre-empted by a shorter one that overlaps it.
- Applied to **every** address, not only failing ones — a correction is a correction, and an address that resolved to the wrong place is worse than one that did not resolve.
- Loaded once per run (`lru_cache`), because `_address_key` runs per row over thousands of rows. **Restart to pick up an edit.**
- The raw source is still what the logs print as `kilde`, so a correction never hides what the caseworker actually wrote. The `normaliseret` line shows the corrected form.

Tracked in git, unlike the other CSVs: it is curated knowledge that should not be lost or re-derived.

**If an address had already resolved to something wrong, delete its row from `resolved_addresses.csv` too** — a cached hit skips this entirely.

### Known special addresses (`_ADRESSE_OVERRIDES`)

Some addresses are written in a form no general normalisation can reach. The case this exists for is "Center for Børne- og Ungehjem", where the source puts the home's own name in front of the street:

```
Toppen, Årslev Møllevej 19, 8220 Brabrand
```

`Toppen` becomes the first comma-component — what the matcher takes for the street — so every prefix is built from it and nothing can match. What is wrong here is the *shape* of the string, not its spelling, so there is nothing for the component logic to work with.

`_ADRESSE_OVERRIDES` maps a substring to the register's wording, and the whole address is replaced before parsing. The homes are a known, finite list, so naming them is both simpler and safer than guessing which leading components are not streets.

Matched through `_fold`, so case, spacing and `æ/ø/å` versus `ae/oe/aa` all work — `Bostedet Toppen, Aarslev Moellevej 19` hits the same entry. Never across a longer house number, so `Årslev Møllevej 19` does not swallow `Årslev Møllevej 190`.

Add a row to the tuple for each new home. The run log reports how many rows were rewritten.

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

### The source *is* the student's address, written differently

```
source   Haurumsvej 13.1.th, 8381 Tilst
CPR      Haurumsvej 13, 1. th, 8381 Tilst

both     haurumsvej131th8381tilst
```

`_vaelg_via_flad_cpr` reduces both to letters and digits — every separator dropped — and takes the student's address when they are the same string.

This is **the only step that does not depend on the search**. Every other one works on rows the register returned, and when the street component is mangled badly enough the search returns nothing, leaving no pool to choose from. Here there is nothing to search for: the source and a known address are simply the same text.

That makes it strong evidence, so it runs before the pool-based CPR step and well before the coordinate guess. It still requires a true miss, and the CPRs behind the address to agree.

It is **not** "use CPR whenever nothing matched". `Holme Byvej 42.` with CPR saying `Kalkærparken 135` stays unresolved — those two flatten to different strings, and the student has simply moved.

Whether it leaves a comment follows the ordinary rule: none where the two differ only in punctuation, which is the usual case here.

### A floor glued on with periods

```
Haurumsvej 13.1.th, 8381 Tilst      register: Haurumsvej 13, 1. th, 8381 Tilst
Holme Byvej 42., 8270 Højbjerg      register: Holme Byvej 42, 8270 Højbjerg
```

A period can stand where a comma or a space belongs, so `_STREET_THEN_FLOOR` accepts `.` as a separator, `_FLOOR_PREFIX` no longer requires whitespace after the floor's own period (`1.th`), and `_street_variants` adds a spelling with trailing punctuation stripped.

`_FLOOR_PREFIX` now requires **either** a period or whitespace after the floor token, so a bare door number like `12` is not read as floor 1 door 2.

`_HOUSE_NUMBER_TAIL` tolerates a trailing period too. Without that the probe could not strip `42.` down to the street, so a failure reported `intet på den vej i det postnummer` for a street that plainly exists — a misleading message, not just a missing hint.

Both resolve exactly, and neither gets a comment: removing whitespace, commas and periods makes source and register identical, so nothing was inferred.

### Floor and door split across components

The register writes floor and door as **one** component; the source sometimes writes them as two, and sometimes hyphenates them:

| source | register |
|---|---|
| `Kamma Klitgårds Gade 107, st, -1` | `Kamma Klitgårds Gade 107, st. 1` |
| `Blomsterlunden 143, 1, -2` | `Blomsterlunden 143, 1. 2` |
| `Langenæs Allé 21, 4-3` | `Langenæs Allé 21, 4. 3` |

Nothing matches while the counts differ, because every source middle must turn up among the candidate's middles and `st` is not `st. 1`. `_saml_etage_doer` merges them into the register's own shape, treating a hyphen inside a component exactly as it treats a comma between two.

Only middles, and only when **every** one of them is a floor-or-door token (a number, or `st`/`kl`/`kld`/`th`/`tv`/`mf`). That is what keeps it off `Kærlundvej 16, Ormslev, 8260 Viby J` and `Bøgebakken 2, 4, Allerslev, 4320 Lejre`, where a middle is a place name.

Worth knowing why this matters beyond the one address that failed: all thirteen of these were misses, and the twelve that appeared to "work" were being rescued by CPR at step 3 — right answer, but only because those students had not moved, and each one flagged for manual review it did not need. Merging makes all thirteen resolve from the source alone.

### A floor with no comma before it

```
Sjællandsgade 95A 1. sal
```

Splitting on commas leaves the whole string as the first component — the one taken for the street — so every prefix carries the floor and nothing can match. The register holds `Sjællandsgade 95A, 1., 8000 Aarhus C`.

`_components` therefore splits the first part where a house number is followed by something floor-shaped: digits, or `st`/`kl`/`kld`. That last condition is load-bearing. Without it, `Egå Mosevej 31 c` would split into a street and a stray `c`, breaking the house-letter case; with it, only a real floor separates.

### "1. sal" is "1."

The source writes the floor out, the register does not:

| source | register |
|---|---|
| `Sjællandsgade 95A, 1. sal` | `Sjællandsgade 95A, 1.` |

`_canon` drops the word, and the search prefixes carry both spellings. Anchored to a leading floor number (`^(\d+)\.?\s*sal\b`), so a street whose name happens to contain "sal" is untouched.

Together these two make `Sjællandsgade 95A 1. sal` resolve **exactly** — one candidate, no assumption, no comment on the kørselsrække. That is worth more than the coordinate fallback would have been: all three flats there share a coordinate, so a guess would have got the position right and the floor wrong, where this gets both right.

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
