# Vyhledávač spojů

Metavyhledávač vlaků a autobusů v Evropě – „Skyscanner pro pozemní dopravu“.
Hledá napříč dopravci, porovnává ceny a umí najít levnější varianty pomocí
tzv. travel hacků (lomené jízdenky, skryté město).

Verze 2 je přepsaná od základu: původní scrapery se nahradily tam, kde to
šlo, přímým voláním API, a všechno je rozdělené do zásuvných modulů.

---

## Proč přepis

Původní kód padal na třech věcech:

| Problém | Příčina | Řešení ve verzi 2 |
|---|---|---|
| Antibot ochrana blokovala scrapery | Vše jelo přes prohlížeč na veřejném webu | 3 ze 4 dopravců volají přímo JSON API, které používá jejich vlastní web – žádný prohlížeč |
| Scrapery se rozbíjely | Natvrdo zadané CSS selektory | API místo DOMu; kde prohlížeč zbyl, jsou selektory vrstvené a selhání je hlášené |
| Špatné časy u nočních spojů | Čas jako `"HH:MM"` a počítání `% 1440` | Všude skutečné `datetime` s časovou zónou |
| Chyby mizely beze stopy | `except Exception: pass` | Každý dopravce vrací `ProviderResult` s chybou; UI ji ukáže |
| Přidat dopravce znamenalo zásah na 5 místech | `ADAPTERS` slovník + `if/elif` větve | Registr pluginů – nový dopravce je jeden soubor |

---

## Instalace

```bash
pip install -r requirements.txt          # jen httpx – stačí pro 3 dopravce
pip install playwright && playwright install chromium   # volitelně, pro ČD
```

Python 3.10+.

## Použití

```bash
# příkazová řádka
python -m vyhledavac Praha Brno
python -m vyhledavac Praha Vídeň --date 01.10.2026 --time 08:00 --isic --sort price
python -m vyhledavac Praha Vídeň --hacks split_ticket --max-price 800 --pareto
python -m vyhledavac --list-providers
python -m vyhledavac Praha Brno --only-priced  # jen spoje se známou cenou
python -m vyhledavac Bratislava Košice --providers ZSSK --card sk_free
python -m vyhledavac Praha Brno --json        # strojově čitelný výstup

# grafické rozhraní
python -m vyhledavac.gui
```

Z Pythonu:

```python
import asyncio, datetime as dt
from vyhledavac import SearchQuery, Passenger, DiscountCard
from vyhledavac.engine import SearchEngine

async def main():
    engine = SearchEngine.build(
        ["RegioJet", "FlixBus", "ÖBB"],
        filter_spec=[("departure_window", {"hours": 8}), "deduplicate", "pareto"],
        hack_names=["split_ticket"],
    )
    query = SearchQuery(
        origin="Praha", destination="Vídeň",
        departure=dt.datetime(2026, 10, 1, 8, 0),
        passengers=(Passenger(age=20, cards=(DiscountCard.ISIC,)),),
    )
    result = await engine.search(query, sort_by="price")
    for journey in result.journeys:
        print(journey.departure, journey.price_label, journey.provider)
    await engine.aclose()

asyncio.run(main())
```

---

## Stav dopravců

Ověřeno živým během v září 2026:

| Dopravce | Způsob | Ceny | Zastávky | Stav |
|---|---|---|---|---|
| **RegioJet** | JSON API (`brn-ybus-pubapi.sa.cz`) | ✅ | ✅ úseky | **ověřeno** – 28 spojů Praha→Brno za ~4 s |
| **FlixBus** | JSON API (`global.api.flixbus.com`) | ✅ | částečně | **ověřeno** – 45 spojů Praha→Brno za ~2 s |
| **ÖBB** | HAFAS (`fahrplan.oebb.at`) | ❌ jen jízdní řád | ✅ **všechny mezizastávky** | **ověřeno** – vrací i české vlaky |
| **ČD** | prohlížeč (Playwright) | ✅ | částečně | opraveno, plný běh neověřen |
| **ZSSK** | prohlížeč (`predaj.zssk.sk`) | ✅ | ne | selektory ověřeny, běh neověřen |
| **OneTicket** | prohlížeč (`oneticket.cz`) | ✅ | ne | vyžaduje viditelné okno |

Ve výchozím nastavení běží jen první tři – jsou rychlé a nepotřebují
prohlížeč. Ostatní zapnete přes `--providers`.

### Poznámka k ČD

ČD nemají veřejné API – formulář posílá POST zpět na stejnou stránku.
Zůstal tedy prohlížeč. Při ladění se našly **dvě konkrétní příčiny**, proč
původní scraper vracel prázdný seznam:

1. **Formulář je na stránce třikrát** (desktop, mobil, sbalená varianta) a
   viditelná je jen jedna kopie. Původní kód bral `.first`, což je skoro
   vždy ta skrytá – kliknutí na ni vyprší. Nově se prochází všechny shody
   a bere se první skutečně viditelná.
2. **Překryvný dialog „Čekejte…“** (`#loaderModal`) odchytává kliknutí.
   Tlačítko je viditelné i povolené, ale klik na něj nedojde. Nově se
   čeká, až dialog zmizí, a když se stránka během kliknutí překreslí,
   rozhoduje se podle toho, jestli dorazily výsledky – ne podle návratové
   hodnoty kliknutí.

S těmito opravami vyhledávání **doběhlo až na stránku s výsledky**
(`/spojeni-a-jizdenka/spojeni-tam/<uuid>`). Kompletní průchod včetně
načtení cen se ale z tohoto prostředí dotáhnout nepodařilo: ČD po několika
dotazech z jedné IP adresy začaly vracet stránku **bez vyhledávacího
formuláře** – hlavička a menu se vykreslí, prostřední část zůstane prázdná.

Provider tenhle stav rozpozná a nahlásí ho jako dočasné omezení, ne jako
změnu selektorů – aby se chyba nehledala v kódu, když je jinde. Z běžné
domácí linky by ČD mělo fungovat; berte ho ale jako **neověřený**.

Pro ceny českých vlaků je ostatně lepší **OneTicket** (jednotný tarif
pokrývá ČD i další dopravce) a jízdní řád českých vlaků včetně všech
mezizastávek vrací **ÖBB přes HAFAS** bez prohlížeče.

### ZSSK

Slovenské vlaky. Selektory z původního kódu (`#fromInput`, `#toInput`,
`#departDate`) jsou **ověřeně stále platné**. Hlavní změna je v cenách:
původní kód u každého spoje proklikal celý nákupní trychtýř (5–6 načtení
stránky na jeden spoj). Nově se ceny nejdřív zkusí přečíst rovnou ze
seznamu a hloubkový režim se pouští jen na vyžádání a jen pro prvních N
spojů:

```bash
python -m vyhledavac Bratislava Košice --providers ZSSK
```

Slovenská 100% sleva se zapíná kartou `sk_free`:
`--card sk_free`. Cena `0 Kč` je platná cena, ne chybějící údaj.

### OneTicket

Prodává jízdenky napříč českými dopravci pod jednotným tarifem, takže je
to **nejlepší zdroj cen českých vlaků** – lepší než scrapovat ČD.

Má zdokumentované REST API (`clientapi.oneticket.cz`, OpenAPI na
`/swagger/source/sjt.yml`). Postup by byl `POST /search` → `pathId` →
`POST /price?method=priceBySearchId`. **Vyhledávání i ceny ale vyžadují
partnerský klíč** – bez něj vrací `401 Auth Invalid header`. Pokud klíč
seženete, cesta přes API se dá doplnit jako `HttpProvider`; všechny
potřebné endpointy jsou popsané v hlavičce `providers/oneticket.py`.

Do té doby jede přes prohlížeč a **potřebuje viditelné okno** – v headless
režimu OneTicket nevykreslí obsah (ověřeno: prázdné `body`). Provider to
pozná a řekne to, místo aby hledal neexistující selektory.

### ÖBB a ceny

ÖBB přes HAFAS vrací jízdní řád, ne ceny. Jejich prodejní API
(`tickets.oebb.at`) při testu z datacentra odpovědělo
*„Zugriff vorübergehend eingeschränkt“* – blokuje IP adresy serveroven.
Z domácí linky může fungovat.

Prázdné místo po cenách ÖBB ale z velké části zaplní **doplňování cen**
(níže) – ÖBB zná vlak, jiný dopravce ho prodává.

---

## Jak je to poskládané

```
vyhledavac/
├── models.py        datový model (datetime, Decimal – žádné "HH:MM" a inf)
├── http.py          odolný async HTTP klient (retry, detekce blokace, limity)
├── browser.py       stealth Playwright – jen pro dopravce bez API
├── cache.py         SQLite cache s TTL (nahradila CSV soubory)
├── money.py         kurzy ČNB
├── normalize.py     porovnávání názvů stanic (diakritika, aliasy)
├── registry.py      registr pluginů
├── engine.py        orchestrace, izolace chyb, průběžné výsledky
├── providers/       dopravci      (regiojet, flixbus, obb, cd)
├── filters/         filtry        (11 vestavěných)
└── hacks/           travel hacky  (split_ticket, hidden_city)
```

### Jak se bráníme blokaci

1. **Nejlepší obrana je nescrapovat.** Weby dopravců samy volají JSON API –
   voláme ho taky. Na API se antibot ochrana typicky nevztahuje.
2. **Konzistentní otisk.** Hlavičky se posílají jako celá sada
   (`User-Agent` + `sec-ch-ua` + `Sec-Fetch-*`), ne jen `User-Agent`.
   Nesourodá kombinace je snadno odhalitelná.
3. **Slušné tempo.** Rozestupy mezi požadavky na stejný host a strop souběhu.
4. **Rozpoznání blokace.** Cloudflare/captcha stránka se pozná i když přijde
   s HTTP 200; klient pak vymění identitu místo opakování téhož.
5. **Backoff s jitterem** a respektování `Retry-After`.
6. Kde prohlížeč zbyl, maskuje `navigator.webdriver`, pluginy, WebGL i
   viditelnost karty a neblokuje se na uspávání záložek na pozadí.

---

## Grafické rozhraní

```bash
python -m vyhledavac.gui
```

Rozhraní je postavené na jednom pravidle: **výpadek jednoho dopravce nesmí
vypadat jako výpadek aplikace.**

- **Výsledky přibývají průběžně**, jak dorazí jednotlivé zdroje – nečeká se
  na nejpomalejšího. FlixBus dodá za 2 s, ÖBB za 3 s, prohlížečový dopravce
  klidně za minutu, a tabulka se plní postupně.
- **Každý zdroj má vlastní kontrolku**: čeká / hledám / hotovo (+ počet
  spojů a čas) / nic nenašel / chyba / blokováno. Je tak vidět, čí data
  v tabulce jsou a čí chybí.
- **Chyby jdou do postranního panelu**, ne do modálního okna. Nic
  nevyskakuje a nic nebrání práci s tím, co už se našlo. U každé chyby je
  věta, se kterou se dá něco dělat („Dopravce vyhodnotil dotaz jako robota“,
  „Chybí prohlížeč – nainstalujte…“), a tlačítko **vypnout** pro ten zdroj.
- **Když selžou všichni**, okno zůstane ovladatelné a vysvětlí proč.
- Zrušení hledání nechá na obrazovce to, co už dorazilo; výsledky
  zrušeného hledání se do tabulky nikdy nedostanou.
- Spoje bez ceny jsou zašedlé, lomené jízdenky žluté, dvojklik otevře trasu
  se všemi zastávkami a rozpisem dílčích jízdenek.

Odolnost je otestovaná – `tests/test_gui.py` ověřuje mimo jiné, že padlý
dopravce nesebere výsledky ostatním a že okno po selhání všech zdrojů
zůstane použitelné. Testy se přeskočí tam, kde není tkinter nebo displej;
pod Xvfb se spustí takto:

```bash
xvfb-run -a python -m pytest tests/test_gui.py
```

## Optimalizace podle ceny

Hledání spojů je jen půlka věci – smysl je vybrat, **čím se vyplatí jet**.

Po každém hledání se vypíše cenový přehled:

```
CENOVÝ PŘEHLED
  nejlevnější  16:01–20:18  4h17m      240 Kč  RegioJet
  nejrychlejší 14:05–18:00  3h55m      459 Kč  FlixBus
  → nejlevnějším spojem ušetříte 219 Kč oproti nejrychlejšímu
  Nejnižší cena podle dopravce: RegioJet od 240 Kč, FlixBus od 339 Kč
  ⚠️ U 5 z 40 spojů cenu neznáme (pokrytí 88%) – podle ceny je neporovnáváte.
```

Co k tomu patří:

- **Nejlepší poměr** (`best_value`) – spoj, který není ani nejdražší, ani
  nejpomalejší. Cena i doba jízdy se normalizují na rozsah nabídky a sečtou.
- **Doplňování cen napříč dopravci** (filtr `price_backfill`). Když jeden
  zdroj zná vlak, ale ne cenu (ÖBB), a jiný ten samý vlak prodává
  (RegioJet), cena se převezme a spoj se označí poznámkou, kde jízdenku
  koupit. Páruje se podle čísla spoje **a** shody odjezdu i příjezdu –
  samotné číslo nestačí, protože „RJ“ znamená u ÖBB Railjet a u RegioJetu
  RegioJet.
- **Pokrytí cenami** – pokud u části spojů cenu neznáme, je to vidět.
  Tiché vydávání neúplné nabídky za úplnou je horší než varování.
- `--only-priced` skryje spoje bez ceny, `--max-price` nasadí strop,
  `--pareto` schová spoje, které jsou zároveň dražší i pomalejší.

Cena `0 Kč` (slovenská 100% sleva) je plnohodnotná cena, ne chybějící údaj.

## Rozšiřování

### Nový dopravce

Jeden soubor ve `vyhledavac/providers/`:

```python
@PROVIDERS.register()
class MujDopravce(HttpProvider):
    name = "MujDopravce"

    async def search(self, query):
        data = await self.client.get_json("https://api.../spojeni", params={...})
        return [Journey(provider=self.name, legs=[...], fares=[...])]
```

Registr ho najde sám. Kompletní ukázka: `examples/novy_dopravce.py`.
Externí plugin mimo balíček jde načíst přes `VYHLEDAVAC_PLUGINS`:

```bash
VYHLEDAVAC_PLUGINS=muj_modul python -m vyhledavac Praha Brno
```

### Nový filtr

```python
@FILTERS.register()
class PouzeDenniSpoje(Filter):
    name = "pouze_denni"

    def apply(self, journeys, query):
        return [j for j in journeys if 6 <= j.departure.hour < 22]
```

Viz `examples/vlastni_filtr.py`.

### Nový travel hack

```python
@HACKS.register()
class MujHack(TravelHack):
    name = "muj_hack"

    async def find(self, query, journeys, search):
        dilci = await search(query.leg_query("Praha", "Břeclav"))
        ...
```

`search` je funkce, kterou si hack doptá libovolný dílčí úsek; engine
výsledky cachuje, takže se stejný dotaz neposílá dvakrát.

---

## Travel hacky

**`split_ticket` – lomená jízdenka.** Zůstanete v jednom vlaku, ale koupíte
dvě jízdenky (A→X a X→B). U přeshraničních cest to často vyjde levněji.
Body lomu se hledají ve **skutečné trase vlaku** (ze seznamu zastávek), ne
jen v pevném seznamu – hraniční stanice mají jen přednost. Body, na které se
nedá koupit jízdenka (státní hranice), se přeskakují.

**`hidden_city` – skryté město.** Jízdenka do vzdálenější stanice může být
levnější; vystoupíte dřív. Nabízí se jen když vlak ve vašem cíli skutečně
staví a nejde o spoj s povinnou místenkou. Výsledek vždy nese varování –
zbytek jízdenky propadá a někteří dopravci to v podmínkách zakazují.

Obě strategie nabídnou variantu jen při úspoře nad nastavený práh
(výchozí 3 %, u skrytého města 8 %).

---

## Testy

```bash
pip install pytest pytest-asyncio
python -m pytest -q
```

85 testů (77 bez GUI). Pokrývají mimo jiné konkrétní chyby původní verze: spoje přes
půlnoc, `limit` aplikovaný před řazením, dva různé vlaky považované za
jeden, nebo chybějící cena vydávaná za nekonečnou.

## Licence a fair play

Nástroj je pro osobní použití. Dotazy jsou omezené tempem, aby dopravcům
nezatěžovaly servery. Ceny a dostupnost vždy ověřte u dopravce –
před koupí se mohou lišit.
