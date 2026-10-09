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

## Instalace, aktualizace a spuštění

**Nepotřebujete žádný API klíč ani registraci.** Viz [Bez API klíčů](#bez-api-klíčů).

Potřebujete jen Python 3.10 nebo novější z
[python.org](https://www.python.org/downloads/). Při instalaci na Windows
zaškrtněte *Add python.exe to PATH*. Verze z Microsoft Storu nestačí.
(*tcl/tk and IDLE* je potřeba jen pro starší okenní rozhraní.)

### Nejjednodušší: dvojklik na `Vyhledavac.bat` (Windows)

Stáhněte projekt (přes `git clone` níže, nebo na GitHubu *Code → Download
ZIP* a rozbalte) a dvakrát klikněte na **`Vyhledavac.bat`**. Při prvním
spuštění si sám připraví prostředí, pak už jen otevírá program – webové
rozhraní ve vašem prohlížeči. Po aktualizaci si sám doinstaluje, co přibylo.

### Příkazy pro Windows (cmd)

**Instalace** – jednou:

```bat
git clone https://github.com/Tomasraketak/Vyhledavac_Spoju.git
cd Vyhledavac_Spoju
py -3 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
```

Volitelně prohlížeč pro České dráhy, ZSSK a OneTicket (~150 MB). Bez něj
fungují RegioJet, FlixBus a ÖBB:

```bat
.venv\Scripts\python -m pip install playwright
.venv\Scripts\python -m playwright install chromium
```

**Spuštění** webového rozhraní (otevře se v prohlížeči):

```bat
cd Vyhledavac_Spoju
.venv\Scripts\pythonw -m vyhledavac.web
```

(`pythonw` neotevře černou konzoli a příkazový řádek se hned vrátí –
program běží na pozadí a za chvíli otevře prohlížeč. Když se spuštění
nepovede, ukáže okno s chybou a celý výpis zapíše do
`vyhledavac-web.log` ve složce programu. Starší okenní rozhraní:
`.venv\Scripts\pythonw -m vyhledavac.gui`.)

Neděje se vůbec nic, ani okno s chybou? Pak Python nenašel modul
`vyhledavac.web` – máte starou verzi programu. Aktualizujte ji (níže)
a pro kontrolu spusťte `.venv\Scripts\python -m vyhledavac.web`
(s konzolí, chybu uvidíte rovnou).

**Aktualizace** na nejnovější verzi:

```bat
cd Vyhledavac_Spoju
git pull
.venv\Scripts\python -m pip install --upgrade -r requirements.txt
```

Máte-li prohlížeč pro ČD, občas aktualizujte i ten:

```bat
.venv\Scripts\python -m pip install --upgrade playwright
.venv\Scripts\python -m playwright install chromium
```

Stáhli jste ZIP místo `git clone`? Aktualizace = stáhnout nový ZIP
a rozbalit přes starou složku (`.venv` se tím nepřepíše), pak dvojklik
na `Vyhledavac.bat`.

### Příkazy pro Linux a macOS

```bash
# instalace
git clone https://github.com/Tomasraketak/Vyhledavac_Spoju.git
cd Vyhledavac_Spoju
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install playwright && .venv/bin/python -m playwright install chromium  # volitelně

# spuštění (webové rozhraní v prohlížeči)
.venv/bin/python -m vyhledavac.web

# aktualizace
git pull && .venv/bin/python -m pip install --upgrade -r requirements.txt
```

Starší okenní rozhraní (`python -m vyhledavac.gui`) potřebuje tkinter,
na Linuxu bývá samostatný balíček: `sudo apt install python3-tk`.

### Bez API klíčů

Oficiální API s klíčem dopravci dávají firmám a prodejcům jízdenek, ne
jednotlivcům. Program je proto postavený tak, aby žádný klíč nepotřeboval
– dívá se na data stejně jako váš prohlížeč, když jste na webu dopravce:

| Dopravce | Jak získává data | Ceny | Potřebuje |
|---|---|---|---|
| RegioJet | veřejné rozhraní, které volá jejich vlastní web | ano | nic |
| FlixBus | veřejné rozhraní, které volá jejich vlastní web | ano | nic |
| ÖBB | veřejný jízdní řád + e-shop s anonymním tokenem (jako návštěvník webu) | ano¹ | nic |
| České dráhy | web cd.cz v prohlížeči na pozadí | ano | Playwright |
| ZSSK | web predaj.zssk.sk v prohlížeči na pozadí | ano | Playwright |
| OneTicket | web oneticket.cz v prohlížeči na pozadí | ano | Playwright |

¹ E-shop ÖBB občas dotazy odmítne (hlavně z VPN a serveroven). Pak zůstane
jízdní řád a ceny se doplní od ostatních dopravců, kteří stejný vlak
prodávají – viz [ÖBB a ceny](#öbb).

Výchozí výběr (RegioJet, FlixBus, ÖBB) funguje hned po instalaci
a nepotřebuje ani prohlížeč. Ostatní si zaškrtnete v GUI v panelu
*Zdroje dat*, až budete mít Playwright.

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

# webové rozhraní (otevře prohlížeč)
python -m vyhledavac.web
python -m vyhledavac.web --port 8080 --no-browser

# starší okenní rozhraní (tkinter)
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

## Dopravci

### Přehled

| Dopravce | Soubor | Jak získává data | Ceny | Mezizastávky | Rychlost | Stav (září 2026) |
|---|---|---|---|---|---|---|
| **RegioJet** | `providers/regiojet.py` | JSON API jejich webu | ✅ | ✅ | ~4 s | ověřeno živě, vč. slev |
| **FlixBus** | `providers/flixbus.py` | JSON API jejich webu | ✅ | částečně | ~2 s | ověřeno živě, vč. slev |
| **ÖBB** | `providers/obb.py` + `_hafas.py` + `_oebb_shop.py` | jízdní řád HAFAS + JSON API e-shopu | ✅¹ | ✅ všechny | ~3 s | jízdní řád ověřen; ceny¹ |
| **ČD** | `providers/cd.py` | web cd.cz v prohlížeči | ✅ | částečně | ~30 s | ověřeno živě, vč. slev |
| **ZSSK** | `providers/zssk.py` | web predaj.zssk.sk v prohlížeči | ✅ | ne | ~30 s + ~8 s/cena | ověřeno živě, vč. slev |
| **OneTicket** | `providers/oneticket.py` | web oneticket.cz v prohlížeči | ✅ | ne | ~20 s | ověřeno živě, vč. slev |

¹ E-shop ÖBB blokuje IP adresy serveroven, takže ceny ÖBB šlo při vývoji
otestovat jen proti nahraným odpovědím. Z domácího připojení by měly
fungovat – viz [ÖBB](#öbb).

Ve výchozím nastavení běží RegioJet, FlixBus a ÖBB – jsou rychlé
a nepotřebují prohlížeč. Ostatní zapnete v GUI v panelu *Zdroje dat*
nebo přes `--providers`.

### Společné pro všechny

Každý dopravce je jeden soubor v `vyhledavac/providers/` s třídou
označenou `@PROVIDERS.register()`. Program ho najde sám. Každý dělá
totéž: dostane `SearchQuery` (odkud, kam, kdy, cestující) a vrátí seznam
`Journey` (úseky, časy, ceny). Liší se jen tím, **odkud** data bere:

- **API dopravci** (`HttpProvider`) volají stejná JSON rozhraní, která
  volá web dopravce. Jsou rychlé a antibot ochrana na ně skoro neplatí –
  posíláme stejné dotazy jako prohlížeč. Obranu (hlavičky jako skutečný
  prohlížeč, cookies, rozestupy mezi dotazy, opakování s čekáním) řeší
  společný `http.py`.
- **Prohlížečoví dopravci** (`BrowserProvider`) ovládají skutečný
  prohlížeč přes Playwright, protože dopravce žádné použitelné API nemá.
  Jsou pomalejší a náchylnější na změny webu. Společný `browser.py`
  maskuje automatizaci, blokuje reklamy a obrázky a pozná ochrannou
  stránku.

Když se modul dopravce vůbec nenačte (chybí knihovna apod.), v nabídce
chybí – GUI ale v panelu vpravo napíše proč. Takhle se našla chyba, kvůli
které na Windows chybělo ÖBB: časové pásmo „Europe/Vienna“ vyžaduje
balíček `tzdata`, který Windows nemá. Teď je v `requirements.txt`
a program má i vlastní záložní pásmo s letním časem.

Chyba jednoho dopravce nikdy neshodí ostatní: engine každého spouští
zvlášť s časovým limitem a chybu ukáže u jeho řádku v GUI. Když dopravce
data dodá jen zčásti (např. jízdní řád bez cen), ukáže se žluté
upozornění místo chyby.

### Cestující a slevy

Z věku a zaškrtnutého ISIC se nejdřív určí **jedna společná kategorie**
(`Passenger.category` v `models.py`) a každý dopravce ji jen přeloží na
svůj kód. Dřív si to každý modul počítal po svém a výsledky se rozcházely.

| Kategorie | Kdo | ČD | RegioJet | FlixBus | OneTicket | ZSSK | ÖBB |
|---|---|---|---|---|---|---|---|
| dítě do 6 | 0–5 let | Dítě 0–5 | `ATTENDED_CHILD` | `children_0_5` | plné | do 15 | dospělý² |
| dítě | 6–14 let | Dítě 6–9 / 10–14 | `CHILD_UNDER_12` / `CZECH_STUDENT_PASS_15` | `children_6_17` | plné | do 15 | dospělý² |
| mládež | 15–17 let | Junior 15–17 | `CZECH_STUDENT_PASS_15` | `children_6_17` | Junior 15–18 | mladý 16–25 | dospělý |
| student | ISIC a věk do 26 | Junior 18–25 + Žákovský průkaz ČR | `CZECH_STUDENT_PASS_26` | `student` | Student 18–26 | mladý + karta ISIC | dospělý |
| dospělý | ostatní (i 18–25 bez ISIC) | Junior 18–25 / Dospělý 26–64 | `REGULAR` | `adult` | Plné jízdné | dospelý | dospělý |
| senior | 65+ | Dospělý 65+ | `ISIC` (sic!) | `senior` | Senior 65+ | senior 62+ | dospělý |

**Studentská sleva** platí se zaškrtnutým ISIC a jen do 26 let – tak to
mají všichni dopravci. ČD tlačítko „ISIC“ nemají; studenta zastupuje
tarif *Junior 18–25 let, Žákovský průkaz ČR* (ISIC s potvrzením o studiu
žákovský průkaz nahrazuje). Bez průkazu stojí Junior 18–25 stejně jako
dospělý – ověřeno na cd.cz. Když zaškrtnete ISIC s věkem 26+, GUI na to
upozorní.

Příklad (Praha → Brno, září 2026): ČD 305 Kč → 152 Kč, RegioJet 199 → 99 Kč,
FlixBus 238 → 123 Kč, OneTicket 537 → 268 Kč, ZSSK Bratislava → Košice
486 → 243 Kč.

Když se slevu u prohlížečového dopravce nepodaří nastavit, program to
napíše žlutě u dopravce – nikdy tiše neukáže cenu za dospělého.

² ÖBB počítá slevy (Vorteilscard, děti) až v košíku; program ukáže cenu
za dospělého a upozorní na to.

---

### RegioJet

**Soubor:** `providers/regiojet.py` · **Typ:** JSON API · **Klíč:** ne

1. Při prvním hledání stáhne číselník měst a stanic (`/consts/locations`,
   ~145 kB) a uloží ho na 7 dní do cache. Souběžná hledání ho stahují jen
   jednou.
2. Název z formuláře najde v číselníku (tolerantně k diakritice a
   překlepům, „Vídeň“ = „Wien“).
3. Zavolá `/routes/search/simple` s kódem tarifu podle kategorie
   cestujícího.
4. U prvních spojů v časovém okně si souběžně stáhne detail
   (`/routes/{id}/simple`) – z něj zná jednotlivé úseky, přestupy
   a cenové třídy (Low cost, Standard, Business…).

**Na co si dát pozor:** kódy tarifů klamou – `ISIC` je u RegioJetu senior
65+, student je `CZECH_STUDENT_PASS_26`. Vyprodaný spoj chodí s cenou 0
a `pricesCount: 0`; program ho ukáže jako *Vyprodáno*, ne jako spoj
zadarmo.

### FlixBus

**Soubor:** `providers/flixbus.py` · **Typ:** JSON API · **Klíč:** ne

1. Přes našeptávač (`/search/autocomplete/cities`) najde ID měst.
2. Zavolá `/search/service/v4/search` se seznamem cestujících
   (`{"student": 1}` apod.). Datum chce ve tvaru `DD.MM.YYYY`.
3. Cena je **včetně poplatku platformy** (`total_with_platform_fee`) –
   tedy to, co skutečně zaplatíte.

**Slevy:** student, senior a mládež 6–17 let mají 50 %, ale jen na
**vnitrostátních** linkách (Praha → Brno). Na mezinárodních (Praha →
Vídeň) stojí stejně jako dospělý – to FlixBus spočítá sám. Když slevovou
kategorii na trase nezná (HTTP 400), program zopakuje dotaz pro
dospělého a upozorní na to. Samotným dětem FlixBus jízdenku neprodá
a API pak vrací nesmyslnou nulu – program cenu v tom případě neukáže.

### ÖBB

**Soubory:** `providers/obb.py`, `_hafas.py`, `_oebb_shop.py` · **Typ:**
dvě API · **Klíč:** ne

ÖBB jsou dva nezávislé zdroje, které běží souběžně:

- **Jízdní řád** z veřejného HAFASu (`fahrplan.oebb.at/bin/mgate.exe`) –
  protokol, na kterém jede většina evropských železnic. Vrací spoje
  **včetně všech mezizastávek**, i pro české vlaky. Na tom stojí travel
  hack „lomená jízdenka“. `_hafas.py` je obecný – další dopravce na HAFASu
  (DB, SBB…) je jen nová konfigurace.
- **Ceny** z JSON API e-shopu `shop.oebbtickets.at`. HAFAS má u ÖBB tarify
  vypnuté, takže ceny zná jen e-shop. Ten dá každému návštěvníkovi
  **anonymní token** (žádná registrace) a s ním se program zeptá:
  `init` → `stations` → `travelActions` → `timetable` → `prices`.
  Ceny se pak přiřadí ke spojům z HAFASu podle času odjezdu a příjezdu.

Před e-shopem stojí Cloudflare, který odmítá **TLS otisk Pythonu** – httpx
dostane 403 bez ohledu na hlavičky. Program se proto ptá přes
[`curl_cffi`](https://github.com/lexiforest/curl_cffi) s otiskem skutečného
Chromu (je v `requirements.txt`; když chybí, zkusí se httpx a pak
prohlížeč).

Tři chyby, kvůli kterým ÖBB dlouho vracelo jen jízdní řád nebo hlášku
„HTTP 404“:

- shop přestal posílat `availabilityState`, a program proto zahazoval
  **každou** cenu;
- prázdné `"datetimeArrival": ""` v dotazu přimělo shop ignorovat datum
  a vracet spoje „od teď“ – nespárovaly se pak s jízdním řádem;
- `init` se zkoušel ve verzích v4 → v5 → v6 a skutečnou chybu z v4
  přepsala 404 z neexistující v6.

Původní skript bral ceny ze stejného e-shopu přes Firefox a klikání do
formuláře, a to občas prošlo. Teď se ptá přímo jeho API:

| Situace | Co uvidíte |
|---|---|
| Všechno funguje | spoje ÖBB s cenami, mezizastávkami a 1. třídou |
| E-shop odmítne přímý dotaz | zkusí se totéž **zevnitř prohlížeče** (je-li nainstalovaný Playwright) |
| E-shop odmítne i prohlížeč, nebo je pomalý (> 45 s) | spoje bez ceny + žluté upozornění; ceny se doplní od jiných dopravců, kteří vlak prodávají |
| Spadne jízdní řád, e-shop odpoví | spoje z e-shopu s cenou, jen bez mezizastávek |
| Spadne obojí | chyba u ÖBB, ostatní dopravci běží dál |

Blokace (*„Zugriff vorübergehend eingeschränkt“*) se týká hlavně IP adres
serveroven a VPN. Z domácí sítě obvykle projde.

### České dráhy

**Soubor:** `providers/cd.py` · **Typ:** prohlížeč · **Klíč:** ne

ČD nemají veřejné API – formulář posílá POST zpět na stránku a výsledek
vykreslí server. Postup:

1. Otevře `cd.cz/spojeni-a-jizdenka/`, počká na doplnění formuláře
   JavaScriptem a zavře cookie lištu.
2. V okně *Cestující* přidá tarif podle kategorie (např. *Junior 18–25
   let, Žákovský průkaz ČR*), odebere výchozího dospělého a v souhrnu
   formuláře ověří, že se tarif opravdu změnil. Dřív hledal tlačítko
   „ISIC“, které na cd.cz není, a potichu nechal dospělého.
3. Vyplní odkud/kam přes našeptávač a odešle.
4. Počká na stránku `/spojeni-tam/…` a z ní přečte spoje a ceny.

Dvě chyby, kvůli kterým původní scraper vracel prázdný seznam:
**formulář je na stránce třikrát** (desktop, mobil, sbalený) a viditelný
je jen jeden – program proto hledá první skutečně viditelnou kopii; a
**dialog „Čekejte…“** zachytává kliknutí – program čeká, až zmizí.

Po mnoha dotazech z jedné IP adresy ČD vrací stránku bez formuláře.
Program to pozná a nahlásí jako dočasnou blokaci, ne jako rozbitý kód.

### ZSSK

**Soubor:** `providers/zssk.py` · **Typ:** prohlížeč · **Klíč:** ne

`predaj.zssk.sk` je JSF aplikace bez API a v seznamu spojů ceny
**neukazuje** – cenu zná až nákupní formulář. Postup:

1. Vyplní odkud/kam. Návrhy našeptávače čte z odpovědi
   `/autocompleteStations/…` a vybere ten, který **opravdu odpovídá**
   zadané stanici (na „Vídeň“ ZSSK jako první nabízí slovenský Vígľaš).
   Zkusí i jiné tvary jména (Viedeň, Wien); když nesedí nic, ZSSK tam
   „nejezdí“.
2. Nastaví datum ve slovenském tvaru `30. 9. 2026` a odešle.
3. Přečte seznam spojů – po jednotlivých vlacích, takže přestupy i noční
   spoje přes půlnoc mají správné časy a data.
4. **Jízdní řád pošle hned** – v rozhraní je vidět za ~5 s.
5. **Ceny:** pro prvních 10 spojů projde *Kúpiť lístok → Voľba cestujúcich*
   *→ Voľba cestovného lístka* a přečte konečnou cenu. Běží to **souběžně
   ve 3 oddělených kontextech prohlížeče** (pro ZSSK tři různí návštěvníci)
   a **každá cena se zobrazí hned**, jak je známá – na poslední spoj se
   nečeká. Ceny se mezi spoji nesdílí (dynamické ceny, zahraniční lístky).

Žádné pevné prodlevy: čeká se na odpověď našeptávače či serveru, výchozí
cestující (dospělý bez slevy) se nepřeklikává a na seznam se vrací otevřením
`/search` (~1 s). Bratislava → Košice: dřív 106 s a cena u 4 z 5 spojů,
teď spoje za 5 s, první cena za ~9 s, všech 5 cen za ~18 s.

Spoj, u kterého formulář nepokračuje (lůžkový vůz, trasa mimo e-shop),
se označí „nelze koupit online“ a nezkouší se znovu. Když ZSSK trasu
neprodává vůbec (Praha → Vídeň), zbytek se přeskočí.

```bash
python -m vyhledavac Bratislava Košice --providers ZSSK
```

Slovenská 100% sleva (studenti a senioři se slovenským nárokem) se zapíná
kartou `sk_free`: `--card sk_free`. Cena `0 Kč` je pak platná cena.

### OneTicket

**Soubor:** `providers/oneticket.py` · **Typ:** prohlížeč · **Klíč:** ne

Prodává jízdenky napříč českými dopravci pod jednotným tarifem (SJT), takže
je to **nejlepší zdroj cen českých vlaků**. Jejich REST API je jen pro
smluvní partnery s klíčem, proto program ovládá veřejný web:

1. Otevře `oneticket.cz/search` a **počká, až React vykreslí formulář**
   (první 1–3 s je stránka prázdná).
2. Vyplní odkud, kam a datum. Pokud našeptávač stanici nezná (zahraničí),
   skončí za pár sekund s „nejezdí tam“ – dřív čekal 45 s a hlásil chybu
   „OneTicket nevrátil žádné spojení“.
3. V okně cestujících vynuluje výchozího dospělého a nastaví tarif podle
   kategorie (Plné, Junior, Student, Senior).
4. Odešle a **přečte JSON, který si stránka sama stáhla** (`/search`,
   `/price`): všechny vlaky spoje, mezizastávky, dopravce. Cena lístku SJT
   platí pro trasu, takže se přiřadí každému spoji po stejné trase. Čtení
   HTML zůstává jako záloha.

Dřívější verze kontrolovala stránku hned po načtení, viděla ji prázdnou
a hlásila chybu – v GUI se to navíc zobrazilo jako „chybí prohlížeč“,
přestože se okno otevřelo. Teď program počká a skrytý prohlížeč stačí,
žádné okno nevyskakuje. Když se stránka nevykreslí ani za 20 s, bývá to
výpadek jejich API a program to tak i napíše.

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
├── stations.py      databáze zastávek: našeptávač, kraj, IDS, tarifní zóna
├── data/            stations.json.gz – sestavuje tools/build_stations.py
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

## Webové rozhraní

```bash
python -m vyhledavac.web          # otevře http://127.0.0.1:8765/ v prohlížeči
```

Na Windows stačí dvakrát kliknout na `Vyhledavac.bat` – viz [Instalace](#nejjednodušší-dvojklik-na-vyhledavacbat-windows).

Malý server ze standardní knihovny (`vyhledavac/web/`), poslouchá jen na
`127.0.0.1`. Stránka je čisté HTML/CSS/JS podle návrhu v design systému
Modernist: ikony jsou vložené, takže funguje i bez internetu (kromě
písma Archivo). Průběh hledání posílá server přes Server-Sent Events.

- **Našeptávač** u polí Odkud/Kam: všechny vlakové stanice a autobusové
  zastávky v ČR (~43 tisíc míst) plus hlavní zahraniční cíle, seřazené
  podle důležitosti (obyvatelé obce + počet spojů) – „Pra“ nabídne Prahu,
  ne zastávku Pražák. Hledá bez diakritiky, v libovolném pořadí slov
  („hl brno“) i podle plných názvů místo zkratek CIS („Královo Pole“ →
  *Brno-Kr. Pole*). U každého místa ukáže kraj, integrovaný systém (PID,
  IDS JMK, IDOL, ODIS…) a tarifní zónu. Ovládá se i šipkami a Enterem.
- **Hledání:** odkud, kam, datum, čas; druhy dopravy (vlak / bus / MHD);
  v *Pokročilých možnostech* počet přestupů, jen přímá spojení, minimální
  čas na přestup, věk a ISIC, travel hacky, „jen se známou cenou“ a
  „skrýt dražší i pomalejší“.
- **Zdroje dat** jsou přepínací čipy se živým stavem (hledám / ceny 3/5… /
  N spojů · čas / nejezdí tam / chyba / blokováno). *Rychlé* = jen zdroje
  bez prohlížeče.
- **Výsledky** přibývají průběžně, ceny pomalých dopravců (ZSSK) se
  doplňují do už zobrazených karet. Řazení (nejrychlejší, nejméně přestupů,
  nejlevnější, nejdřívější) a filtry podle dopravců se přepnou okamžitě,
  bez nového hledání.
- **Karta spoje:** časová osa úseků barevně podle druhu dopravy, přestupy
  (kratší než 3 min označené jako riziko), cena a zdroj. *Detail* ukáže
  itinerář s nácestnými zastávkami, u nástupní a výstupní stanice kraj,
  IDS a zónu (v souhrnu *Tarifní zóny* odkud → kam), souhrn a dílčí
  jízdenky lomené jízdenky; *Koupit* otevře web dopravce.
- **Co se nepovedlo:** panel s vysvětlením a tlačítkem *vypnout*. Nic
  nevyskakuje.
- CZK / EUR, světlý / tmavý motiv, funguje i na mobilu.

Server se sám ukončí, když se stránka 2 minuty neozve (`--idle 0` vypne),
a druhé spuštění jen otevře už běžící instanci.

### Databáze zastávek

`vyhledavac/data/stations.json.gz` (~0,6 MB) je součástí repozitáře, za
běhu se nic nestahuje. Obnovit ji (třeba s novým jízdním řádem v prosinci)
jde jedním příkazem – stáhne ~150 MB zdrojů do `.cache/stations/`:

```bash
python tools/build_stations.py            # --refresh = stáhnout zdroje znovu
```

| Údaj | Zdroj |
|---|---|
| autobusové zastávky, okres, počet spojů | CIS JŘ – JDF, [portal.cisjr.cz](https://portal.cisjr.cz/pub/jdf/) |
| vlakové stanice a zastávky, počet vlaků | CIS JŘ – CZPTT, [portal.cisjr.cz](https://portal.cisjr.cz/pub/draha/celostatni/szdc/) |
| kraj stanic, plné názvy a počty obyvatel obcí | © přispěvatelé [OpenStreetMap](https://www.openstreetmap.org/copyright) (ODbL), Overpass API |
| pásma PID | [data.pid.cz](https://pid.cz/o-systemu/opendata/) (CC BY 4.0) |
| zóny IDS JMK | GTFS [KORDIS JMK](https://kordis-jmk.cz/gtfs/gtfs.zip) |

Kraj → systém: Praha a Středočeský PID, Jihočeský IDS JK, Plzeňský IDPK,
Karlovarský IDS KK, Ústecký DÚK, Liberecký IDOL, Královéhradecký
a Pardubický IREDO, Vysočina VDV, Jihomoravský IDS JMK, Olomoucký IDSOK,
Zlínský IDZK, Moravskoslezský ODIS. Zóny volně zveřejňují jen PID
a IDS JMK; u ostatních systémů se ukáže kraj a systém bez zóny.

## Okenní rozhraní (tkinter)

```bash
python -m vyhledavac.gui
```

Původní rozhraní zůstává. Stejně jako webové je postavené na jednom pravidle: **výpadek jednoho dopravce nesmí
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

Přes 170 testů. Pokrývají mimo jiné konkrétní chyby původní verze: spoje přes
půlnoc, `limit` aplikovaný před řazením, dva různé vlaky považované za
jeden, nebo chybějící cena vydávaná za nekonečnou.

## Licence a fair play

Nástroj je pro osobní použití. Dotazy jsou omezené tempem, aby dopravcům
nezatěžovaly servery. Ceny a dostupnost vždy ověřte u dopravce –
před koupí se mohou lišit.
