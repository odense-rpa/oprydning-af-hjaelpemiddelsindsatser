# Oprydning af hjælpemiddelsindsatser

Robotten rydder op i hjælpemiddelsindsatser i KMD Nexus for borgere tilknyttet Hjælpemiddelservice — lukker eller annullerer udløbne/inaktive indsatser og fjerner organisationstilknytningen for borgere uden aktive udlån.

## Hvad gør robotten?

1. Henter alle borgere tilknyttet organisationen 'Hjælpemiddelservice' i KMD Nexus og lægger dem i arbejdskøen (--queue).
2. For hver borger i arbejdskøen hentes det fulde borgerdata fra Nexus.
3. Kontrollerer om borgeren har aktive udlån uden en tilknyttet indsats (kurv). Sådanne tilfælde rapporteres til rapporteringssystemet som 'Borgere med udlån uden indsats'.
4. Finder alle kurvindsatser under forløbene 'Sundhedsfagligt grundforløb' og 'Ældre og sundhedsfagligt grundforløb'.
5. For hver indsats fra Hjælpemiddelservice der ikke allerede er lukket, annulleret eller afvist, vurderes det om indsatsen skal lukkes:
   - Spring over, hvis der er aktive udlån knyttet til indsatsen.
   - Spring over, hvis indsatsens slutdato er i fremtiden.
   - Spring over, hvis indsatsen er bevilliget inden for de seneste 31 dage.
6. Indsatser i status 'Bestilt' eller 'Ændret' lukkes (slutdato sættes). Indsatser i status 'Bevilliget' annulleres.
7. Hvis borgeren slet ingen aktive udlån har, fjernes organisationstilknytningen til 'Hjælpemiddelservice'.
8. Alle behandlede borgere rapporteres til rapporteringssystemet som 'Kontrollerede borgere'.

## Forudsætninger

- Python ≥ 3.13
- [`uv`](https://docs.astral.sh/uv/) til pakkehåndtering
- Adgang til **Automation Server** (arbejdskø og credentials)
- Adgang til **KMD Nexus** (produktion)
- Adgang til **Odense SQL Server** (rapportering)

## Installation

```sh
uv sync
```

## Konfiguration

Credentials registreres i Automation Server:

- `KMD Nexus - produktion`
- `Odense SQL Server`
- `RoboA`

## Kørsel

```sh
uv run python main.py --queue   # Fyld arbejdskøen med borgere fra Hjælpemiddelservice
uv run python main.py           # Behandl borgere i arbejdskøen
```

## Afhængigheder

| Pakke | Formål |
|---|---|
| `automation-server-client` | Klient til Automation Server (arbejdskøer, credentials, workitems) |
| `kmd-nexus-client` | Klient til KMD Nexus (borgere, udlån, indsatser, organisationer) |
| `odk-tools` | Opgavesporing (Tracker) og rapportering |
| `python-dateutil` | Dato-parsing og -manipulation |

## GDPR og sikkerhed

Robotten behandler CPR-numre for borgere tilknyttet Hjælpemiddelservice. CPR-numre gemmes midlertidigt i arbejdskøen i Automation Server og indgår i rapporter for både borgere med udlån uden indsats og alle kontrollerede borgere. Adgang til arbejdskøen og rapporterne bør begrænses til medarbejdere med tjenstligt behov.
