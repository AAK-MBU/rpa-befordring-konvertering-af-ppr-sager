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

   `Bevilling.adresse_id` fortæller, hvor den enkelte bevilling blev givet, og en elev, der er flyttet, har ældre bevillinger på den gamle adresse — derfor slås den op pr. bevilling og ikke pr. sag.

   **Rækkerne i én bevilling kan være uenige:** en klubrække har klubben stående, hvor de andre har hjemmet, og en bevilling, der spænder over en flytning, har både den gamle og den nye adresse. Derfor sendes alle de adresser, rækkerne slog op, videre som `adresse_id_kandidater`, og valget træffes i `bevilling_creation`, som er det eneste sted med elevens egne data.

   Elevens egen adresse afgør:

   - **Matcher en af kandidaterne `Elev.adresse_id`** → den kommer på bevillingen. Data er korrekte, og statusmotoren rører den ikke.
   - **Matcher ingen** → første opslåede adresse beholdes. Den er forskellig fra elevens, så `usp_recalculate_bevilling_status` sætter `adresse_mismatch = 1` og sender bevillingen til **genbehandling af sig selv** — flaget skal ikke tvinges, for en forkert adresse *er* netop den uoverensstemmelse, den leder efter.

   De fravalgte rækker går ikke tabt: hver er stadig sin egen kørselsrække, og en klubrække bærer sine rå værdier med i kommentaren.

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
| alle opslagsværdier | — | matches med al whitespace fjernet: kilden skriver `Egenbefordring`, hvor tabellen har `Egen befordring`. Kontrolleret, at ingen to værdier i nogen opslagstabel falder sammen, når mellemrum fjernes. Matcher en værdi først efter normalisering, logges begge stavemåder |
| `ElevensAdresse` / `SkoleNavnBefordring` | kørselsrækkens `kommentar` | nævner en af dem en klub (`_KLUB_MARKERS`), tilføjes de rå værdier til kommentaren. Det gamle system havde ikke klubkørsel, så felterne blev brugt som erstatning — kun de oprindelige værdier viser, hvad rækken egentlig dækkede |
| `BevillingAfKoerselstype` | `befordringstype_id` + `tillaeg_ids` | kilden slår de to sammen: `Rutekørsel fast forsæde` bliver til kørselstypen `Rutekørsel` og tillægget `Fast forsæde`. Direkte match forsøges først, så en almindelig kørselstype aldrig deles ved en fejl |
| `TidspunktForBevilling` | `tidspunkt_id` **og** `rutetype_id` | samme kolonne styrer begge: Morgen → Hjem til skole, Eftermiddag → Skole til hjem, Morgen og eftermiddag → Mellem hjem og skole |
| `Revurdering` | `revurderingsdato` | en dato i fortiden nulstilles, så en konverteret bevilling ikke straks markeres til revurdering |
| *(bruges ikke)* | `sagsbehandler_id` | fast sat til `_SAGSBEHANDLER_NAVN` (p.t. "Sofie"). Kildens `Sagsbehandler`-kolonne kasseres — navnene er gamle PPR-medarbejdere og passer ikke med `Sagsbehandler`-tabellen, som indeholder rigtige, nuværende medarbejdere |
| nyeste `Modified` i gruppen | `sagsbehandlingsdato` | hvornår bevillingen sidst blev behandlet — den nyeste af gruppens rækker, ikke den første |
| `CaseID` | `esdh_noegle` | PPR-sagens id, som også bruges til dublettjek |

## Alle konverteringskommentarer er mærket

Hver kommentar, robotten skriver på en kørselsrække, begynder med `KONVERTERING-PPR | <type>`. Intet andet i applikationen skriver den tekst, så hele listen over konverterede bevillinger, der skal ses efter, er ét opslag:

```sql
SELECT DISTINCT b.bevilling_id, b.cpr_elev, b.esdh_noegle, k.koersel_id, k.kommentar
FROM   befordring.Koersel   k
JOIN   befordring.Bevilling b ON b.bevilling_id = k.bevilling_id
WHERE  k.kommentar LIKE '%KONVERTERING-PPR%'
ORDER  BY b.cpr_elev, b.bevilling_id;
```

Typen står efter lodret streg: `adressematch`, `adresse valgt via CPR`, `adresse rettet via CPR`, `adresse antaget — manglende etage/dør`, `lukket sag — elevens nuværende adresse` og `klub i kildedata`. En kørselsrække kan have flere, og sagsbehandlerens egen tekst bevares øverst.

## Uløste adresser til sagsbehandlerne

Hver kørsel skriver de adresser, der ikke kunne slås op, til `uloeste_adresser.csv` og `uloeste_adresser.xlsx` — samme rækker i begge. Kolonner: PPR-sag, CPR, adressen som den står i BefordringsData, hvor CPR har eleven boende, årsagen, og hvad registret har på vejen.

Én række pr. sag og elev bag hver adresse, sorteret efter sag. Filerne overskrives hver kørsel, så en adresse, der bliver rettet i kilden, forsvinder af sig selv.

Filtrér først på `aarsag`: en **klubadresse** er forventet og er ikke arbejde — den bevilling oprettes på elevens egen adresse.

## Manuelle adresserettelser

`adresse_erstatninger.csv` med kolonnerne `Find` og `Erstat` retter formuleringer, ingen regel kan udlede — fx et forkortet vejnavn:

| Find | Erstat |
|---|---|
| `I. Christensens Gade` | `Inger Christensens Gade` |

Tilføj en linje og kør igen; der skal ikke ændres kode. Der matches uden hensyn til store/små bogstaver og mellemrum, længste frase først, og rettelsen bruges på alle adresser — ikke kun dem, der fejler. Filen læses én gang pr. kørsel, så en ændring kræver en genstart.

Loggens `kilde` viser stadig den rå kildeadresse, så en rettelse aldrig skjuler, hvad der faktisk stod. Havde adressen allerede slået op på noget forkert, skal dens linje også slettes fra `resolved_addresses.csv`.

## Kendte særadresser

Nogle adresser er skrevet på en måde, ingen generel normalisering kan nå. Det gælder "Center for Børne- og Ungehjem", hvor kilden skriver hjemmets eget navn foran vejen — `Toppen, Årslev Møllevej 19, 8220 Brabrand`. `Toppen` bliver dermed det første komma-led, altså det matcheren tager for vejnavnet, og så kan intet passe.

`_ADRESSE_OVERRIDES` i `processes/queue_handler.py` oversætter den slags til registrets egen skrivemåde, før adressen overhovedet bliver delt op. Tilføj en linje pr. nyt hjem. Kørslen logger, hvor mange rækker der blev skrevet om.

## Når CPR afgør en flertydig adresse

Registret har tit både adgangsadressen og enhedsadressen — `Øster Kringelvej 25` og `Øster Kringelvej 25, st.` er samme bolig skrevet to gange — og kilden siger `25`, hvilket passer på begge. Her slår robotten op i CPR: passer præcis én af kandidaterne med elevens egen adresse, vælges den, og valget skrives i loggen med adressens navn.

Det sker kun ved flertydighed, aldrig når intet passede: ville man tage CPR-adressen dér, opfandt man en adresse, kilden ikke bakker op om. Og kun når de elever, der deler adressen, peger på den samme kandidat.

## Lukkede PPR-sager

`BefordringsData` fortæller ikke, om en sag er lukket, så listen trækkes manuelt fra ESDH til `Lukkede foranstaltningsmapper.csv` med kolonnerne `Sags ID` og `Status` — kun rækker med `Lukket` tæller. Stien står i `helpers/config.py`.

Kan en adresse på en **lukket** sag ikke slås op, oprettes bevillingen på elevens nuværende adresse fra LOIS i stedet for at blive afvist, og hver kørselsrække får en kommentar om det. En lukket sag kan ikke rettes, og dens bevilling er ikke aktiv — så en upræcis adresse koster ingenting, mens en tabt række ikke kan genskabes.

Det sker kun, når ingen af bevillingens rækker kunne slås op, kun for sager i filen, og kun når LOIS kender eleven. Mangler filen, behandles ingen sag som lukket.

## Manglende etage, løst på koordinater

En kildeadresse uden etage og dør passer på alle lejligheder i opgangen — og registret giver dem alle **samme koordinat**, ét punkt for bygningen. Koordinaten er det, systemet faktisk bruger: gåafstand til skole og ruteplanlægning. Derfor vælges den første af dem, og kørselsrækken får en kommentar om, at etagen manglede, hvilken bolig der blev antaget, og at den skal rettes manuelt.

Det er en antagelse, ikke et svar, så det prøves først efter CPR-opslaget. Er koordinaterne forskellige, afvises adressen som før — forskellige punkter er forskellige steder.

## Cache af opslåede adresser

Adresseopslaget er den langsomme del af kø-fasen, og langt de fleste af de ~3700 rækker rammer plet første gang. Derfor gemmes hvert vellykket opslag i `resolved_addresses.csv`, som næste kørsel læser først — så en gentagen kørsel, der kun skal se på de få fejlende adresser, ikke betaler for alle de andre igen.

Kun vellykkede opslag gemmes; fejl prøves igen hver gang. Filen skrives løbende, så en kørsel, der dør undervejs, beholder det, den nåede. Den er gitignored.

**Slet filen, når matchreglerne ændres** — et cache-hit springer matcheren helt over, så en linje skrevet under de gamle regler ville overleve den rettelse, der skulle fange den. Sæt `RESOLVED_ADDRESS_CACHE = None` i `helpers/config.py` for at slå cachen fra.

## Miljøvariabler (`.env`)

| Variabel | Formål |
|---|---|
| `ATS_URL`, `ATS_TOKEN` | Automation Server-køen |
| `ATS_WORKQUEUE_OVERRIDE` | Overskriv workqueue-id (dev/test) |
| `API_ENDPOINT` | Base-URL på befordrings-API'et |
| `API_KEY` | Sendes som `X-API-Key`. Skal matche en hash i miljøets `API_KEY_HASHES` |
| `DBCONNECTIONSTRINGSERVER29` | LOIS, kun læsning. Samme variabel og samme værdi som i `rpa-befordring-nightly-runs`. **Valgfri** — bruges kun til at vise, hvor CPR har eleven boende, når en adresse ikke kunne slås op. Er den ikke sat, mangler den ene linje i advarslen, og ellers ændrer intet sig |

Forbindelsen til RPA-databasen (`BefordringsData`) er **ikke** en miljøvariabel — den hentes fra `RPAConnection` under kørsel. `DBCONNECTIONSTRINGSERVER29` er en anden forbindelse til en anden server.

## Hvis konverteringen skal genoptages

Dublettjekket bruger `(esdh_noegle, foerste_koersel_dato)`. Alle bevillinger fra samme sag har samme `esdh_noegle`, så den alene kan kun svare på, om sagen har *nogen* bevilling — en kørsel, der døde efter den første af tre, ville ved næste forsøg springe alle tre over.

En bevilling oprettes *før* sine kørselsrækker, så en kørsel, der døde imellem de to, efterlader en bevilling uden rækker — stående på Påbegyndt uden indhold. Derfor er "findes den?" det forkerte spørgsmål: robotten tjekker, om den rent faktisk har kørselsrækker, og færdiggør den, hvis ikke, frem for at springe over eller oprette en dublet.

Alle kørselsrækker slås desuden op og valideres, *før* bevillingen oprettes. Tidligere skete opslaget inde i POST-løkken, altså efter bevillingen fandtes, så én uoversættelig værdi efterlod en tom bevilling — præcis det, kildens stavemåde `Egenbefordring` udløste. Nu fejler sagen uden at skrive noget.

To bevillinger i samme sag kan dog stadig begynde samme dag, og så kan de ikke skelnes bagefter. En ren kørsel konverterer begge korrekt; kun en genoptaget kørsel ville springe den anden over. Kø-fasen logger de sager, det gælder — tjek dem manuelt, hvis konverteringen genstartes undervejs.

## Før go-live

Punkterne står udførligt i `CLAUDE.md`. Kort fortalt:

- Elever oprettes ikke længere af denne robot. Alle nuværende elever ligger allerede i `Elev`, fordi nattekørslen indlæser hele elevudtrækket. Mangler et CPR, kender nattekørslen ikke personen — typisk fordi vedkommende er flyttet eller færdig med skolen — og sagen parkeres til manuel vurdering i stedet for at der oprettes en tom elevrække, som intet siden ville udfylde.
- Adresseopslaget er ikke afprøvet mod hele datasættet. De to systemer skriver ikke adresser ens, og forskellen er ikke kun én ting: registret har LOIS' `SupplBynavn` med (`Kærlundvej 16, Ormslev, 8260 Viby J`), hvor de gamle data ikke har — men etage og dør (`Langkærvej 19, st. tv, ...`) står i begge og afgør, hvilken lejlighed der er tale om. Derfor sammenlignes vej og postnummer eksakt, mens de mellemliggende led matches som delsekvens: ekstra led hos kandidaten er i orden, manglende led er ikke. Mangler etage og dør i de gamle data, rammer opslaget alle lejligheder på adressen, og sagen afvises frem for at gætte. De to systemer sætter også tegn forskelligt i den samme adresse: `Drosbjerg 16, 1 th` mod registrets `Drosbjerg 16, 1. th`, og `Egå Mosevej 31 c` mod `Egå Mosevej 31C`. Ved sammenligning fjernes alle punktummer og mellemrum, så begge par falder sammen — og da en adresse kun accepteres ved præcis ét match, ender to registerrækker, der falder sammen, i manuel opfølgning frem for at blive gættet imellem. Selve søgningen er et præfiks-opslag, så den prøver begge stavemåder af både husnummer og etage, mest præcise først.

Hvert kø-emne logges undervejs med sine adresser: kilden som den står i BefordringsData, og den adresse den matchede i `Adresse` — eller `— INTET MATCH —`. Begge sider skrives ud, fordi de to systemer sætter tegn forskelligt, og et match, der ser forkert ud ved første øjekast, som regel ikke er det.

Første `--queue`-kørsel logger desuden, hvor stor en andel der kunne slås op, og lister resten med antal kandidater.
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
