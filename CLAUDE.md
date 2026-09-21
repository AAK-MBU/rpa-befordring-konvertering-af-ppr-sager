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
2. Resolves every distinct student address to an `adresse_id` against the befordring application's **own** `Adresse` table, through `/adresse/by-tekst` and `/adresse/search`. That table is a full copy of the municipality's register, refreshed nightly by `rpa-befordring-nightly-runs`, so this bot no longer reaches into LOIS itself and the whole conversion is API-only. Addresses that resolve to nothing, or ambiguously, are logged and left unresolved — `process_item` then raises a `BusinessError` for manual follow-up rather than creating a student with no address.
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

### Address resolution is unverified against real data

`/adresse/by-tekst` is an **exact, case-sensitive** match on `"<adresse>, <postnummer>"`, and nobody has yet confirmed that `BefordringsData.ElevensAdresse` / `ElevensPostnummer` spell an address the same way `Adresse.adresse_tekst` does (which comes from LOIS `AdresseBetegnelse`, e.g. `"Grøndalsvej 1, 8260 Viby J"` — note the postal code carries a city name).

The prefix-search fallback exists to absorb that, and it is deliberately strict: a candidate is accepted only when exactly one survives filtering by postal code, because `"Grøndalsvej 1"` is also a prefix of `"Grøndalsvej 10"`. Placing a child at the wrong house is worse than failing to place them.

The first `--queue` run logs the resolution rate and lists every unresolved address. Read that before trusting the conversion — a low rate means the two systems format addresses differently, and the fix is normalisation in `_resolve_adresse_ids`, not a larger fallback.

## Verified compatible

Checked against the befordring backend as of migration 022. These are fine and need no change:

- `ansoegningstype` now sends `"Fast kørsel"` (was `"Kørsel"`, retired by migration 009). Legacy PPR bevillinger are all standing arrangements, so that is the correct half of the split.
- `KoerselsraekkeCreateRequest` requires only `gyldig_fra`, `gyldig_til`, `tidspunkt_id`, `befordringstype_id`; all four are sent.
- `begrundelse_fra_formular` became nullable in migration 017. Before that, a bevilling whose hjemmel was not in `_HJEMMEL_MAPPING` would have been rejected, because the bot strips `None` from the payload.
- Both hjemmel targets — `§ 26, stk. 1 afstand` and `§ 26, stk. 2 sygdom` — exist verbatim in `seed_lookup_data.sql`.
- The `sfo` → `institution` rename (migration 013) does not affect this bot: it sends neither field.
