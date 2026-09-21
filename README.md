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
2. Slår op, hvilken adresse **hver bevilling er givet på**, i befordringsapplikationens egen `Adresse`-tabel via `/adresse/search`. Den tabel er en fuld kopi af kommunens adresseregister, som `rpa-befordring-nightly-runs` opdaterer hver nat — så robotten henter ikke længere selv fra LOIS, og hele konverteringen går gennem ét API.

   Det er ikke elevens nuværende adresse: den ligger allerede på `Elev` fra nattekørslen. `Bevilling.adresse_id` fortæller, hvor den enkelte bevilling blev givet, og en elev, der er flyttet, har ældre bevillinger på den gamle adresse — derfor slås den op pr. bevilling og ikke pr. sag.

   `Bevilling.adresse_id` er `NOT NULL`, så en bevilling uden match kan ikke oprettes. `process_item` afviser hele sagen i det tilfælde frem for at konvertere den delvist.
3. Danner **ét kø-item pr. PPR-sag** med `CaseID` som reference, så `--queue` kan køres igen uden at skabe dubletter.
4. Fordeler rækkerne inden for sagen på bevillinger efter **hvornår de gælder**:

| Gruppe | Regel | Resultat |
|---|---|---|
| `current` | perioden overlapper `[i dag, vinduets slutning]` | **én** bevilling — beregnes som Aktiv |
| `future` | perioden begynder efter vinduet | **én** bevilling — beregnes som Kommende |
| `past` | allerede udløbet | én bevilling **pr. periode** — Udløbet, som ikke kolliderer |
| `ukendt` | datoer mangler eller kan ikke læses | holdes for sig og logges |

   Grunden til at de aktuelle rækker slås sammen: `usp_recalculate_bevilling_status` sætter en borger med mere end én **Aktiv** bevilling til Fejlet. To overlappende gamle rækker ville ramme netop det og vælte hele sagen.

   Vinduet slutter en måned efter kørselsdatoen, eller på `config.CONVERSION_WINDOW_END`, hvis den er sat. Sæt den, når konverteringsdatoen er aftalt — så giver en gentaget kørsel samme gruppering.

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
| `TidspunktForBevilling` | `tidspunkt_id` **og** `rutetype_id` | samme kolonne styrer begge: Morgen → Hjem til skole, Eftermiddag → Skole til hjem, Morgen og eftermiddag → Mellem hjem og skole |
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

## Hvis konverteringen skal genoptages

Dublettjekket bruger `(esdh_noegle, foerste_koersel_dato)`. Alle bevillinger fra samme sag har samme `esdh_noegle`, så den alene kan kun svare på, om sagen har *nogen* bevilling — en kørsel, der døde efter den første af tre, ville ved næste forsøg springe alle tre over.

To bevillinger i samme sag kan dog stadig begynde samme dag, og så kan de ikke skelnes bagefter. En ren kørsel konverterer begge korrekt; kun en genoptaget kørsel ville springe den anden over. Kø-fasen logger de sager, det gælder — tjek dem manuelt, hvis konverteringen genstartes undervejs.

## Før go-live

Punkterne står udførligt i `CLAUDE.md`. Kort fortalt:

- Elever oprettes ikke længere af denne robot. Alle nuværende elever ligger allerede i `Elev`, fordi nattekørslen indlæser hele elevudtrækket. Mangler et CPR, kender nattekørslen ikke personen — typisk fordi vedkommende er flyttet eller færdig med skolen — og sagen parkeres til manuel vurdering i stedet for at der oprettes en tom elevrække, som intet siden ville udfylde.
- Adresseopslaget er ikke afprøvet mod hele datasættet. De to systemer skriver ikke adresser ens, og forskellen er ikke kun én ting: registret har LOIS' `SupplBynavn` med (`Kærlundvej 16, Ormslev, 8260 Viby J`), hvor de gamle data ikke har — men etage og dør (`Langkærvej 19, st. tv, ...`) står i begge og afgør, hvilken lejlighed der er tale om. Derfor sammenlignes vej og postnummer eksakt, mens de mellemliggende led matches som delsekvens: ekstra led hos kandidaten er i orden, manglende led er ikke. Mangler etage og dør i de gamle data, rammer opslaget alle lejligheder på adressen, og sagen afvises frem for at gætte. Første `--queue`-kørsel logger, hvor stor en andel der kunne slås op, og lister resten med antal kandidater.
- `TOP (10)` står stadig i forespørgslen.
- Ugedage sættes altid til "Alle". Kilden har dem ikke, så hver konverteret kørselsrække gælder alle skoledage. Det passer for en fast ordning, men ikke hvor to kørselstyper deler ugen mellem sig — de sager skal en sagsbehandler se på.
- `view_Student_Bevillinger` skal gendeployes: `foerste_koersel_dato` er tilføjet til viewet, og uden den matcher dublettjekket aldrig.
- SSL-bypass-blokken i `main.py` er udkommenteret, men står der endnu — den bør slettes.

## Udvikling

```bash
uv run ruff check .
uv run ruff format .
```

GitHub Actions kører `ruff check` ved hvert push og pull request, og en PR mod `main` afvises, hvis `version` i `pyproject.toml` ikke er hævet.
