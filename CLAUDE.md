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

   Note this is *not* the student's current address — that already sits on `Elev`, put there by the nightly run, and nothing here looks it up. `Bevilling.adresse_id` records where a given bevilling was granted, which is what `adresse_mismatch` later compares against the student's own. A student who moved has older bevillinger at the previous address, so it is resolved **per bevilling**, not per case.

   `Bevilling.adresse_id` is `NOT NULL`, so a bevilling whose address cannot be matched cannot be created. `process_item` rejects the whole case in that event rather than converting it partially — a case missing one of its bevillinger looks complete to a caseworker and is harder to spot than one that never arrived.
3. Groups rows into **one queue item per PPR case**, referenced by `CaseID`, so re-running `--queue` cannot duplicate.
4. Within a case, groups rows into **bevillinger by `(BevillingFra, BevillingTil)`**. Rows sharing a date pair are kørselsrækker of one bevilling; a different date pair is a different, often outdated, bevilling.

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
| `CaseID` | `esdh_noegle` | the PPR case id, also the de-duplication key |

## Environment variables (`.env`)

| Variable | Purpose |
|---|---|
| `ATS_URL`, `ATS_TOKEN` | Automation Server workqueue |
| `ATS_WORKQUEUE_OVERRIDE` | Override the workqueue id (dev/test) |
| `API_ENDPOINT` | Base URL of the befordring API |
| `API_KEY` | Sent as `X-API-Key`. Must match a hash in the target environment's `API_KEY_HASHES` — see that repo's `.env.example` |

The RPA database connection string is **not** an env var; it is fetched at runtime from `RPAConnection`.

## Known issues

Ordered by how much they matter at go-live.

1. **The de-duplication check cannot resume a partial run.** `existing_esdh_noegler` is fetched once, before the loop, and every bevilling in a case shares the same `esdh_noegle` (the PPR case id). So if a case has three bevillinger and the run dies after the first, the retry sees that `esdh_noegle` already present and skips **all three** — including the two never created. Within a single clean run it behaves correctly, because the set is not refreshed mid-loop.

2. **`groupby` is applied to unsorted rows.** `itertools.groupby` only groups *consecutive* equal keys. The query sorts by `CaseID` alone, so within a case the rows are in arbitrary order — a case whose rows run date-pair A, B, A yields three bevillinger instead of two. Sort each case's rows by `(BevillingFra, BevillingTil)` before the inner `groupby`.

3. **`TOP (10)`** is still in the query, marked as test mode. A real conversion would import ten cases.

4. **Kørselsrækker are created without `dag_ids` or `rutetype_id`.** Both are optional server-side so the write succeeds, but the application's own form requires them — the first caseworker to edit a converted kørselsrække cannot save it until they fill in weekdays and rutetype.

5. **`main.py` still carries the SSL-bypass block** under the `🔥 REMOVE BEFORE DEPLOYMENT` banner. It is currently commented out; delete it rather than leave it to be uncommented by accident.

6. `process_item()` keeps a vestigial `bor_case_id = ppr_case_id` and a docstring describing a "Step 1: GO conversion" that no longer exists.

### Students are never created here

Every currently enrolled student is already in `Elev`: `rpa-befordring-nightly-runs` upserts the whole dump from `Elev_STG` long before this conversion runs. A CPR that is missing is therefore one the nightly load does not know — they have most likely left the municipality or finished school since the legacy bevilling was written.

This bot used to `POST /citizen/create_elev` with just `{cpr, adresse_id}`. That produced a row with no name, no `skolekode` and no `elevklassetrin`, which nothing would ever fill in: the nightly upsert only touches CPRs present in `Elev_STG`, and this one is not. It would also sit outside the school derivation permanently, because `usp_sync_elev_matrikel_from_bevilling` requires a non-zero `skolekode` before it will take a matrikel from the bevilling — and no school means no walking distance either.

So a miss now raises `BusinessError`, which sends the item to `pending_user` rather than failing it. Nothing the bot can do resolves it; a person has to decide whether that student should be converted at all.

### How addresses are matched

The two systems do not write an address the same way:

```
BefordringsData   Kærlundvej 16, 8260 Viby J
Adresse           Kærlundvej 16, Ormslev, 8260 Viby J
```

Same address. The register carries LOIS's `SupplBynavn` (`Ormslev`) where one exists; the legacy data does not. Note also that `ElevensAdresse` **already includes the postcode and city** — `ElevensPostnummer` is a separate column holding `8260` again, and appending it produces nonsense.

So both sides are reduced to `(street + number, postcode)` by `_split_adresse()` — first comma-component and the four digits at the start of the last one — and everything in between is ignored. `_matches()` then compares case-insensitively on street, exactly on postcode.

Lookup is `/adresse/search` with **`"<street>,"` including the trailing comma** as the prefix. Every `adresse_tekst` has a comma straight after the house number, so `"Kærlundvej 16,"` matches `"Kærlundvej 16, Ormslev, 8260 Viby J"` but not `"Kærlundvej 160, ..."` or `"Kærlundvej 16A, ..."`. Without the comma, searching for house 1 pulls in 1, 10, 11 and the rest — and that endpoint caps at 15 rows, so the wanted one can be pushed out entirely.

A candidate is accepted only when **exactly one** survives. Placing a bevilling at the wrong address is worse than failing to place it.

Still unverified against the full dataset: the first `--queue` run logs the resolution rate and lists every unresolved address with its candidate count. Read it before trusting the conversion. A `0 candidate(s)` line means the address is absent or the street is spelled differently; `2+` means the trailing-comma trick did not disambiguate and that address needs looking at by hand.

## Verified compatible

Checked against the befordring backend as of migration 022. These are fine and need no change:

- `ansoegningstype` now sends `"Fast kørsel"` (was `"Kørsel"`, retired by migration 009). Legacy PPR bevillinger are all standing arrangements, so that is the correct half of the split.
- `KoerselsraekkeCreateRequest` requires only `gyldig_fra`, `gyldig_til`, `tidspunkt_id`, `befordringstype_id`; all four are sent.
- `begrundelse_fra_formular` became nullable in migration 017. Before that, a bevilling whose hjemmel was not in `_HJEMMEL_MAPPING` would have been rejected, because the bot strips `None` from the payload.
- Both hjemmel targets — `§ 26, stk. 1 afstand` and `§ 26, stk. 2 sygdom` — exist verbatim in `seed_lookup_data.sql`.
- The `sfo` → `institution` rename (migration 013) does not affect this bot: it sends neither field.
