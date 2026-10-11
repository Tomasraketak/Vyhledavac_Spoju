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
prodávají – viz [ÖBB a ceny](docs/DOPRAVCI.md#öbb).

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

**Podrobně ke každému dopravci** (adresy API, postup, cestující, ceny, kdy
se ptá a na co si dát pozor): **[docs/DOPRAVCI.md](docs/DOPRAVCI.md)**.

| Dopravce | Soubor | Jak získává data | Ceny | Mezizastávky | Kdy se ptá | Stav (říjen 2026) |
|---|---|---|---|---|---|---|
| **RegioJet** | `providers/regiojet.py` | JSON API jejich webu | ✅ skupina | ✅ | vždy | ověřeno živě, vč. slev |
| **FlixBus** | `providers/flixbus.py` | JSON API jejich webu | ✅ skupina | ne | vždy | ověřeno živě, vč. slev |
| **ÖBB** | `providers/obb.py` + `_hafas.py` + `_oebb_shop.py` | jízdní řád HAFAS + JSON API e-shopu | ✅¹ | ✅ všechny | ne po Česku | jízdní řád ověřen; ceny¹ |
| **ČD** | `providers/cd.py` | web cd.cz v prohlížeči | ✅ skupina | částečně | ne na zastávky bez nádraží | ověřeno živě, vč. slev |
| **ZSSK** | `providers/zssk.py` | web predaj.zssk.sk v prohlížeči | ✅ | ne | ne po Česku | ověřeno živě, vč. slev |
| **OneTicket** | `providers/oneticket.py` | web oneticket.cz v prohlížeči | ✅ skupina | ne | jen Česko | ověřeno živě, vč. slev |
| **Bileto** | `providers/bileto.py` | JSON API portálu bileto.cz | ✅ skupina | ne | jen po ČR | ověřeno živě |
| **Arriva** | `providers/arriva.py` | JSON API e-shopu shop.arriva.cz | ✅ skupina | ✅ | jen po ČR | ověřeno živě |
| **Leo Express** | `providers/leo.py` | GraphQL API leoexpress.com | ✅ skupina | ✅ | jen jeho stanice | ověřeno živě |
| **IDS** | `providers/ids_idos.py` + `idos.py` | spoje z IDOS, cena z tarifu IDS | ✅² | ✅ | jeden IDS nebo zastávka bez nádraží | ověřeno živě |
| **PKP** | `providers/pkp.py` | JSON API Koleo (koleo.pl) | ✅ 1 dospělý | ✅ | konec v Polsku | ověřeno živě |
| **MÁV** | `providers/mav.py` | JSON API e-shopu jegy.mav.hu | ✅ skupina | ne | konec v Maďarsku | ověřeno živě |
| **DB** | `providers/db.py` | JSON API int.bahn.de (+ prohlížeč) | ✅ skupina³ | ✅ | konec v DE/NL/BE/FR/CH/DK/LU | stanice ověřeny, spoje³ |
| **NS** | `providers/ns.py` | JSON API plánovače ns.nl | ✅ jen po NL | ✅ | konec v Nizozemsku | ověřeno živě |
| **Trenitalia** | `providers/trenitalia.py` | JSON API lefrecce.it | ✅ jen po Itálii⁴ | ne | konec v Itálii | ověřeno živě |
| **OUIGO** | `providers/ouigo.py` | JSON API (Francie i Španělsko) | ✅ skupina | ne | konec ve FR/BE nebo ES | ověřeno živě |
| **Renfe** | `providers/renfe.py` | formulář + DWR venta.renfe.com | ✅⁵ | ne | konec ve Španělsku | ověřeno živě |
| **Eurostar** | `providers/eurostar.py` | GraphQL site-api.eurostar.com | ✅ skupina | ne | oba konce v síti Eurostaru | ověřeno živě |
| **European Sleeper** | `providers/european_sleeper.py` | JSON API e-shopu | ✅ skupina | ne | obě stanice na jeho trase | ověřeno živě |
| **CFR** | `providers/cfr.py` | HTML formuláře bilete.cfrcalatori.ro | ✅⁵ | ✅ | konec v Rumunsku, jen vnitrostátně | ověřeno živě |
| **SŽ** | `providers/sz.py` | HTML e-shopu eshop.sz.si | ✅ skupina | ✅ | jen po Slovinsku | ověřeno živě |
| **HŽPP** | `providers/hzpp.py` | JSON API prodaja.hzpp.hr | ✅⁶ | ne | jen po Chorvatsku | ověřeno živě |
| **SNCF** | `providers/sncf.py` | JSON API srovnávače Virail (ceny od Komba/Omia) | ✅⁷ | ne | konec ve Francii | API ověřeno živě |
| Italo | – | – | – | – | – | nezapojeno: přihlášení chrání Akamai |

¹ E-shop ÖBB blokuje IP adresy serveroven, takže ceny ÖBB šlo při vývoji
otestovat jen proti nahraným odpovědím. Z domácího připojení by měly
fungovat.
² Cena spočítaná z tarifu integrovaného systému, ne od prodejce.
³ int.bahn.de chrání Akamai: z datacentra prošlo vyhledání stanic, dotaz na
spoje ne (ani v prohlížeči). Z domácího připojení by měl projít; zpracování
odpovědi je podle formátu db-vendo-client.
⁴ U mezinárodních spojů vrací Trenitalia jen orientační cenu nebo cenu za
italskou část trasy – ta se jako cena spoje nebere.
⁵ Dopravce vrací cenu za jednoho cestujícího; cena skupiny se dopočítává
podle jeho pravidel (děti, mládež).
⁶ Dětské jízdné online není – děti 6–11 let se počítají jako dospělí (horní
odhad, spoj nese poznámku).
⁷ Přímo SNCF i Trainline chrání captcha DataDome, proto přes srovnávač
Virail. Cena je od agregátoru; u skupiny jen orientační (neověřeno).

Ve výchozím nastavení běží všichni dopravci bez prohlížeče: RegioJet,
FlixBus, ÖBB, Bileto, Arriva, PKP, MÁV, DB, NS, Trenitalia, OUIGO, SNCF, Renfe,
Eurostar, European Sleeper, CFR, SŽ, HŽPP, Leo Express a IDS. Zahraniční se ale ptají jen na cesty do své země, takže hledání po
Česku nezdržují. Bileto a Arriva se naopak ptají jen na cesty po Česku. ČD, ZSSK a OneTicket (prohlížeč) zapnete v panelu *Zdroje
dat* nebo přes `--providers`.

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

### Jednotliví dopravci

Postup u každého dopravce (RegioJet, FlixBus, ÖBB, ČD, ZSSK, OneTicket,
Bileto, Arriva, Leo Express, IDS, PKP, MÁV, DB, NS, Trenitalia, OUIGO, SNCF, Renfe,
Eurostar, European Sleeper, CFR, SŽ, HŽPP), včetně
chyb, na které jsme narazili, je v **[docs/DOPRAVCI.md](docs/DOPRAVCI.md)**.

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
├── providers/       dopravci      (22 – viz docs/DOPRAVCI.md)
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
  IDS JMK, IDESKA, IDOL, ODIS…) a tarifní zónu. Ovládá se i šipkami a Enterem.
- **Hledání:** odkud, kam, datum, čas; druhy dopravy (vlak / bus / MHD);
  v *Pokročilých možnostech* počet přestupů, jen přímá spojení, minimální
  čas na přestup, travel hacky, „jen se známou cenou“ a „skrýt dražší
  i pomalejší“.
- **Cestující** (tlačítko vedle druhů dopravy, např. *21 let · student
  s ISIC* nebo *3 cestující · 2× dospělý, dítě 6–14 let*): libovolná
  skupina až 9 cestujících jako u ČD – každý má svůj věk, ISIC a slovenskou
  bezplatnou přepravu (100% zľava). Každý dopravce podle toho zvolí tarif –
  RegioJet Student 18–25, ČD Junior / Dítě / Dospělý 65+ (do okna *Cestující*
  na cd.cz přidá celou skupinu), FlixBus/OneTicket student, ZSSK kategorii,
  kartu ISIC i bezplatnou jízdenku. Cena je za celou skupinu; jízdenky IDS
  se počítají pro každého zvlášť a sečtou (dítě do 6 let zdarma, děti
  a senioři v Praze v PID zdarma…). ZSSK a ÖBB umějí cenu jen pro jednoho
  cestujícího – stránka na to upozorní. Nastavení si stránka pamatuje.
- **Předplatné jízdenky IDS** (v *Pokročilých možnostech*): komu, jaký
  systém a které zóny má předplacené (časový kupón; `*` = celá síť),
  třeba „PID · P, 0, B“ nebo „IDS JMK · 100, 101“. Jízdenky IDS pak uvnitř
  předplatného nic nestojí a mimo něj se kupuje jen doplatek za zóny za
  hranicí předplatného (i ty, kterými vlak jen projede): Praha → Beroun
  s kupónem na Prahu = jízdenka na pásma 1–4 za 48 Kč místo 84 Kč. Kódy zón
  ukazuje našeptávač u zastávek.
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
- **Diagnostika** (rozbalovací pole pod výsledky): běžící verze (větev,
  commit), cesta k záznamu serveru, které zdroje server zná a které jsou
  zapnuté, moduly, které se nenačetly, a jak dopadl každý zdroj při
  posledním hledání. Tlačítko *kopírovat* ho dá do schránky.
- CZK / EUR, světlý / tmavý motiv, funguje i na mobilu.

Server se sám ukončí, když se stránka 2 minuty neozve (`--idle 0` vypne).
Druhé spuštění otevře už běžící instanci – pokud ta ale běží se starším
kódem (třeba z doby před `git pull`), spouštěč ji ukončí a pustí novou;
otevřená stránka se pak sama obnoví.

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
| zóny IDESKA (IDS JK) | oficiální [mapa zón](https://kalkulacka.ideska.info/view.html?view) – mnohoúhelníky; zastávka podle polohy v OSM, jinak středu obce |
| tarif IDESKA, zastávky přesahů | [kalkulačka IDESKA](https://kalkulacka.ideska.info/) (`jikord.json`) → `data/ids_ideska.json.gz` |
| železniční síť (kudy vlaky jezdí) | CIS JŘ – CZPTT → `data/rail.json.gz` |

Kraj → systém: Praha a Středočeský PID, Jihočeský IDESKA (IDS JK), Plzeňský IDPK,
Karlovarský IDS KK, Ústecký DÚK, Liberecký IDOL, Královéhradecký
a Pardubický IREDO, Vysočina VDV, Jihomoravský IDS JMK, Olomoucký IDSOK,
Zlínský IDZK, Moravskoslezský ODIS. Zóny volně zveřejňují PID, IDS JMK
a IDESKA; u ostatních systémů se ukáže kraj a systém bez zóny.

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

**`ids_ticket` – jízdenky integrovaných systémů místo ČD.** Na regionálních
vlacích ČD (Os, Sp, R) platí i jízdenky IDS a bývají výrazně levnější. Jízdenka
IDESKA platí ve 2. třídě i na dálkových vlacích ČD Ex/IC (Jižní expres) na
tratích 190/191, 194, 196, 220 (až do Benešova u Prahy), 225 a 226. Na EC
a railjety jízdenky IDS neplatí. ČD je
prodávají jen v aplikaci Můj vlak – webový e-shop je nenabízí (ověřeno), proto
se ceny IDS počítají z tarifů systémů offline. Hack u každého vlaku ČD zjistí
skutečnou trasu (zastávky a časy z detailu vlaku na cd.cz, jinak ze sítě
sestavené z celostátního jízdního řádu) a vybere nejlevnější z variant:

1. celá cesta na jízdenku jednoho systému;
2. řetěz systémů s lomem ve stanicích, které patří do dvou systémů
   (České Budějovice → Plzeň: IDESKA do Horažďovic, dál IDPK; Žďár nad
   Sázavou → Brno: VDV do Křižanova, dál IDS JMK); nejlevnější řetěz hledá
   dynamické programování;
3. systém + ČD s lomem v **poslední stanici, kde systém ještě platí**
   (přesahy jsou v datech: IDESKA do Počátek-Žirovnice, Benešova u Prahy,
   Nepomuku, Horažďovic…), zbytek ocení ČD; nebo obráceně.

Výsledek vypíše, že se nepřestupuje, jen kupují dvě jízdenky – odkud kam,
u koho, za kolik, a o kolik je to levnější než jedna jízdenka ČD.

| Systém | Tarif | Zóny stanic |
|---|---|---|
| IDESKA (Jihočeský kraj) | celý tarif z [kalkulačky IDESKA](https://kalkulacka.ideska.info/) | seznam zastávek kalkulačky + mapa zón |
| PID (Praha, Středočeský) | ceník podle počtu pásem z [pid.cz](https://pid.cz/jizdne-a-tarif/) ([Tarif 2026](https://pid.cz/neplatvicnezmusis/): P, 0, B, 1–13 navazují; Praha se pro cestu do regionu počítá jako 3 pásma). Jen po Praze: děti 6–15 a senioři 65+ zdarma, sleva jen senioři 60–65, junioři a studenti platí plné; v regionu sleva pro děti, juniory, studenty a seniory 65+ | data PID |
| VDV (Vysočina) | za běhu z [tarifního počítadla kraje](https://www.kr-vysocina.cz/spocitat-jizdne/ms-122447) (cena podle výchozí a cílové zóny, pamatuje se) | zastávky a nádraží z mapy počítadla, i přesahy (Počátky-Žirovnice) |
| IREDO (Královéhradecký, Pardubický) | za běhu z [tarifního kalkulátoru IREDO](https://iredo.info/tarifni-kalkulator/) (stejné počítadlo jako VDV) | zastávky a nádraží z mapy kalkulátoru |
| IDS JMK (Jihomoravský) | [Tarif IDS JMK od 1. 10. 2026](https://content.idsjmk.cz/cenik/261001/Tarif.pdf): cena podle počtu projetých zón, ceny aplikace POSEIDON (2 zóny 22 Kč … všechny 102 Kč), zlevněná A polovina, senior 70+ 24 h za 40 Kč | zóny z GTFS IDS JMK, nádraží podle nejbližší zastávky |
| IDSOK (Olomoucký) | Tarif IDSOK od 1. 10. 2026, Ceník A a B (elektronické ceny): podle počtu projetých zón; přes Olomouc vlakem stačí regionální zóna 710 (Ceník A), s MHD Olomouc Ceník B | zóny linek IDSOK z jízdních řádů CIS JŘ (JDF, „I71“), nádraží podle zastávky u nádraží |
| DÚK (Ústecký) | za běhu z API [kalkulačky DÚK](https://kalkulator.dopravauk.cz/) (cena podle zón, nejlevnější z hotovost / karta / aplikace) | zastávky a nádraží (kódy SR70) z kalkulačky, i přesahy (Česká Lípa, Mělník, Kralupy) |
| IDZK (Zlínský) | za běhu z [kalkulačky IDZK](https://idzk.info/kalkulator-ng/) („tarif nové generace“ – pevná cena mezi tarifními body, zlevněné polovina) | tarifní body kalkulačky, i přesahy (Kojetín, Veselí nad Moravou) |
| IDOL (Liberecký) | za běhu z [tarifního počítadla IDOL](https://www.iidol.cz/jizdne-a-tarify/tarifni-pocitadlo/) (cena při platbě kartou) | seznam zastávek počítadla, nádraží podle obce / názvu zóny |
| IDPK (Plzeňský kraj) | vzorec z Tarifu IDPK (příl. 4 Smlouvy PET IDPK, 1. 7. 2026): 14/12/10 Kč za zónu, Plzeň 18 Kč, síťová 84 Kč | pásma linek IDPK z jízdních řádů CIS JŘ; počítají se **všechny projeté** zóny, i kde vlak nestaví |

Příklady (12. 10. 2026, ověřeno proti ČD): Třeboň → Havlíčkův Brod R 653
ČD 269 Kč, IDESKA do Počátek-Žirovnice 81 Kč + VDV 94 Kč = **175 Kč**;
České Budějovice → Plzeň R 638 (student s ISIC) ČD 102 Kč, IDESKA do
Horažďovic 49 Kč + IDPK (6 zón do Plzně) 40 Kč = **89 Kč**.

**Lom jen tam, kde vlak staví.** Podle SPPO ČD musí mít cestující jízdenku
aspoň do nejbližší stanice, kde jeho vlak zastavuje – jízdenka končící ve
stanici, kterou vlak projíždí (např. Kralice nad Oslavou u rychlíku Náměšť
n. O. – Brno), nestačí. Hack proto lomí jen ve stanicích, kde zastavení
potvrdil jízdní řád: trasa vlaku z cd.cz, zastávky spoje ČD, tentýž vlak
u jiného dopravce (Arriva vrací celou trasu i přes změnu čísla vlaku).
Trasa ze železniční sítě (když vlak cestou mění číslo) slouží jen
k výpočtu projetých zón.

Potřebuje zapnutý zdroj ČD (cena k porovnání a případně cena úseku u ČD).
**ODIS** má jen zóny (z jízdních řádů, ukazují se v našeptávači a detailu):
jízdné REGION se od 2025 počítá z tarifních kilometrů (5 Kč + 0,75 Kč/km),
které pro trasu vlaku veřejně dostupné nejsou – úseky v ODIS proto ocení ČD.
IDS KK (Karlovarský kraj) se záměrně nepočítá – úseky tam ocení ČD. Každý cestující skupiny dostane svou jízdenku
(dítě do 6 let zdarma, děti / studenti / senioři zvýhodněné podle pravidel
systému) a ceny se sečtou. Počítadla VDV, IREDO, IDOL, DÚK a IDZK se volají za běhu (z vašeho počítače),
proto u nich ceny chybí, když je počítadlo nedostupné.

Strategie nabídnou variantu jen při úspoře nad nastavený práh
(výchozí 3 %, u skrytého města 8 %).

### ÖBB jen na část trasy (`obb_partial`)

Shop ÖBB u některých spojů prodá jízdenku jen na úsek („Ticket nur für
Teilstrecke“, např. Třeboň → Marchegg 14,90 € Sparschiene, ale ne do
Bratislavy). Taková cena se u celého spoje nezobrazí – byla by nesmyslně nízká;
spoj má poznámku „ÖBB prodá jízdenku jen na úsek …“. Hack pak dokoupí zbytek
samostatnou jízdenkou (i ÖBB) a nabídne kombinaci, pokud je levnější.

### Přestup v Praze / Brně mezi dopravci (`hub_transfer`)

Vede-li spoj přes Prahu nebo Brno, zkusí hack úseky *odkud → město*
a *město → kam* u všech dopravců a spáruje je přes přejezd po městě
(`citytransfer.py`): Plzeň → Liberec = FlixBus/ČD do Prahy, metro na Černý
Most, RegioJet do Liberce. Přejezdy (odhad ode dveří ke dveřím + 5 min
rezerva): Praha – metro B a C podle jízdních dob (Zličín, Černý Most,
Florenc, Ládví, Kobylisy, Roztyly, hl. n., Holešovice, Smíchov, Masarykovo;
Libeň a Zahradní Město bez metra), jízdenka PID 30/90 min (děti a senioři
v Praze zdarma), hl. n. – Florenc pěšky. Brno – Grand – hl. n. – Dolní
nádraží pěšky, Zvonařka, Židenice, Královo Pole tramvají (IDS JMK).
Nabídne se jen kombinace dvou různých dopravců, která je levnější než
nejlevnější přímý spoj, nebo rychlejší než nejrychlejší a ne dražší.

### Včasná jízdenka Evropa ČD (`cd_europe_border`, `cd_europe_beyond`)

Akční jízdenka ČD je levná jen z Česka do jedné (akční) země. Proto:

* **cesta do třetí země** (Třeboň → Bratislava přes Vídeň): jízdenka ČD
  jen na poslední stanici tranzitní země (Marchegg/Kittsee, Nickelsdorf,
  Štúrovo …), dál ÖBB; případně ještě lom na první stanici cílové země (ÖBB
  přes hranici, ZSSK po Slovensku). Ověřeno: Třeboň → Marchegg 349 Kč +
  Marchegg → Bratislava 109 Kč.
* **trať Salzburg – Linz – Wien**: jízdenka ČD až do Horního Dvořiště (přes
  Linz) nebo Břeclavi (přes Vídeň), vystoupíte dřív. Ověřeno: Wien → Linz
  s jízdenkou do Horního Dvořiště 411 Kč.

Podmínky ČD: jízdenka je vázaná na vytištěný vlak mezi ČR a sousední zemí
(musí se použít v celé jeho trase), platí 2 dny, kontingent je omezený –
hack na to upozorní; vystoupení dřív je na riziko cestujícího.

### Regionální síťové jízdenky (`regional_pass`)

Jízdenka ČD na první stanici za hranicí a dál **Einfach-Raus-Ticket**
(Rakousko, 46 € pro 2–5 osob, jen R/REX/S/CJX) nebo **Bayern-Ticket**
(34 € + 10 € za další osobu, RB/RE/S/ALX…), obojí ve všední dny od 9:00.
Nabídne se jen, když zbytek nalezeného spoje jede výhradně vlaky, ve kterých
síťová jízdenka platí (ne RJ/IC/EC/ICE – např. EC Praha – Mnichov ne).

### Šetrnost k dopravcům

Hacky běží v pořadí od levných po ty s prohlížečem (IDS, lomená jízdenka,
přestup v Praze/Brně, skryté město, Včasná Evropa, síťové jízdenky). Každý
se ptá jen tehdy, když trasa podmínky splňuje (zastávka na trase, země,
kategorie vlaků), dílčí dotazy se sdílí přes cache a dotaz na úsek jde jen
na dopravce, kteří ho můžou prodat (`only=["ČD"]` …). Na ČD se každý hack
ptá nejvýš jednou až dvakrát.

**Cesty jen po Česku** se neposílají na ÖBB a ZSSK (jen zdržují; vnitrostátní
spoje a ceny mají čeští dopravci). V přehledu zdrojů u nich stojí „jen zahraničí“.

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
