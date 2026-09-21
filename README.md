# rpa-befordring-konvertering-af-ppr-sager

Engangsrobot, der konverterer de eksisterende befordringsdata til bevillinger i den nye applikation, *Befordringssystemet*. Den kører ved go-live — ikke efter en tidsplan.

Kilden er `[RPA].[rpa].[BefordringsData]`. Målet er Befordringssystemets REST API. Robotten skriver ikke direkte i databasen; alt går gennem API'et, så de samme valideringer og forretningsregler gælder som for en sagsbehandler i brugerfladen.

En tidligere udgave journaliserede også PPR-dokumenter i GetOrganized. Det er fjernet (`79fd8fb`) sammen med `helpers/case_handler.py` og `helpers/document_handler.py`. Intet i dette repo taler længere med GetOrganized.

## Kom i gang

```bash
uv sync

python main.py --queue      # læs BefordringsData, gruppér, fyld workqueue
python main.py --process    # opret bevillinger via befordrings-API'et
python main.py --finalize   # stub
```

Faserne er uafhængige og kan kombineres.

## Sådan hænger det sammen

### `--queue`

`retrieve_items_for_queue()` i `processes/queue_handler.py` står for hele grupperingen:

1. Læser `BefordringsData` fra RPA-databasen (forbindelsen hentes fra `RPAConnection`).
2. Slår `adresse_id` op for hver enkelt adresse i befordringsapplikationens **egen** `Adresse`-tabel via `/adresse/by-tekst` og `/adresse/search`. Den tabel er en fuld kopi af kommunens adresseregister, som `rpa-befordring-nightly-runs` opdaterer hver nat — så denne robot henter ikke længere selv fra LOIS, og hele konverteringen går gennem ét API. Adresser, der ikke kan slås entydigt op, logges, og `process_item` afviser de sager med en `BusinessError` til manuel opfølgning.
3. Danner **ét kø-item pr. PPR-sag** med `CaseID` som reference, så `--queue` kan køres igen uden at skabe dubletter.
4. Grupperer rækkerne inden for sagen til **bevillinger efter `(BevillingFra, BevillingTil)`**. Rækker med samme datopar er kørselsrækker under samme bevilling; et andet datopar er en anden — ofte forældet — bevilling.

### `--process`

`processes/bevilling_creation.py` kalder API'et i denne rækkefølge:

| Trin | Kald |
|---|---|
| 1 | Henter fem opslagslister (`tidspunkter`, `koerselstyper`, `hjemler`, `sagsbehandlere`, `skolematrikel`) én gang |
| 2 | `GET /citizen/stamdata/{cpr}` — eleven **skal** findes i forvejen; ellers `BusinessError` |
| 3 | `GET /bevilling/get_student_bevillinger/{cpr}` for at undgå dubletter |
| 4 | `POST /bevilling/create_bevilling/{cpr}` pr. bevilling |
| 5 | `POST /bevilling/create_koerselsraekke/{bevilling_id}` pr. kørselsrække |

Alle kald autentificeres med `X-API-Key`.

### Oversættelser

| Kilde | Mål | Hvordan |
|---|---|---|
| `SkoleID` | `matrikel_id` | `/lookup/skolematrikel` returnerer `skolekode`, netop så denne robot kan bygge opslaget uden en ekstra forespørgsel |
| `HjemmelForBevilling` | `hjemmel_id` + `begrundelse_fra_formular` | `_HJEMMEL_MAPPING` — de tre værdier, de gamle data faktisk indeholder |
| `Revurdering` | `revurderingsdato` | en dato i fortiden nulstilles, så en konverteret bevilling ikke straks markeres til revurdering |
| `CaseID` | `esdh_noegle` | PPR-sagens id, som også bruges til dublettjek |

## Miljøvariabler (`.env`)

| Variabel | Formål |
|---|---|
| `ATS_URL`, `ATS_TOKEN` | Automation Server-køen |
| `ATS_WORKQUEUE_OVERRIDE` | Overskriv workqueue-id (dev/test) |
| `API_ENDPOINT` | Base-URL på befordrings-API'et |
| `API_KEY` | Sendes som `X-API-Key`. Skal matche en hash i miljøets `API_KEY_HASHES` |

Forbindelsen til RPA-databasen er **ikke** en miljøvariabel — den hentes fra `RPAConnection` under kørsel.

## Før go-live

Punkterne står udførligt i `CLAUDE.md`. Kort fortalt:

- Elever oprettes ikke længere af denne robot. Alle nuværende elever ligger allerede i `Elev`, fordi nattekørslen indlæser hele elevudtrækket. Mangler et CPR, kender nattekørslen ikke personen — typisk fordi vedkommende er flyttet eller færdig med skolen — og sagen parkeres til manuel vurdering i stedet for at der oprettes en tom elevrække, som intet siden ville udfylde.
- Adresseopslaget er endnu ikke afprøvet mod rigtige data. `/adresse/by-tekst` matcher eksakt og versalfølsomt, og det er ikke bekræftet, at `ElevensAdresse` / `ElevensPostnummer` staves som `adresse_tekst`. Første `--queue`-kørsel logger, hvor stor en andel der kunne slås op, og lister resten — læs den, før konverteringen sættes i gang.
- Dublettjekket kan ikke genoptage en delvist gennemført kørsel: alle bevillinger i en sag deler samme `esdh_noegle`, så et forsøg nummer to springer dem alle over, også dem der aldrig blev oprettet.
- `groupby` køres på usorterede rækker og kan derfor splitte én bevilling i flere.
- `TOP (10)` står stadig i forespørgslen.
- Kørselsrækker oprettes uden `dag_ids` og `rutetype_id`, som brugerfladen kræver, når rækken senere skal redigeres.
- SSL-bypass-blokken i `main.py` er udkommenteret, men står der endnu — den bør slettes.

## Udvikling

```bash
uv run ruff check .
uv run ruff format .
```

GitHub Actions kører `ruff check` ved hvert push og pull request, og en PR mod `main` afvises, hvis `version` i `pyproject.toml` ikke er hævet.
