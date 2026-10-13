# Dopravci – jak program získává spojení a ceny

U každého dopravce tu najdete:
- odkud bere data (adresy API, postup);
- jak hledá stanice;
- jak počítá cestující a slevy;
- jakou cenu vrací (měna, za koho, co obsahuje);
- kdy se ho program vůbec ptá;
- na co si dát pozor.

Na konci je přehled dopravců, které zapojit nešlo (Italo), a jak dopravce
přidat.

Stav „ověřeno živě“ znamená, že při vývoji (říjen 2026) prošlo skutečné
hledání proti webu dopravce. Weby se mění. Když dopravce přestane
fungovat, ukáže to panel *Co se nepovedlo* a pole *Diagnostika* ve webovém
rozhraní.

## Obsah

- [Společná pravidla](#společná-pravidla)
- [Přehled](#přehled)
- České a středoevropské:
  [RegioJet](#regiojet) · [FlixBus](#flixbus) · [České dráhy](#české-dráhy) ·
  [OneTicket](#oneticket) · [Arriva](#arriva) · [Bileto](#bileto) ·
  [Leo Express](#leo-express) · [IDS (z IDOS)](#ids-z-idos)
- Zahraniční:
  [ÖBB](#öbb) · [ZSSK](#zssk) · [PKP (Koleo)](#pkp-koleo) · [MÁV](#máv) ·
  [DB](#db--deutsche-bahn) · [NS](#ns--nizozemské-dráhy) ·
  [Trenitalia](#trenitalia) · [OUIGO](#ouigo) · [SNCF](#sncf-přes-kombo) · [Renfe](#renfe--španělské-dráhy) ·
  [Eurostar](#eurostar) · [European Sleeper](#european-sleeper) ·
  [CFR](#cfr-călători--rumunské-dráhy) · [SŽ](#sž--slovinské-dráhy) ·
  [HŽPP](#hžpp--chorvatské-dráhy) · [Vy](#vy--norské-dráhy) · [DSB](#dsb--dánské-dráhy) ·
  [SJ](#sj--švédské-dráhy) · [BDŽ](#bdž--bulharské-dráhy) · [CP](#cp--portugalské-dráhy) ·
  [Srbijavoz](#srbijavoz--srbské-dráhy) · [SBB](#sbb--švýcarské-dráhy)
- [Nezapojení dopravci (Italo, Trainline, SNCF Connect)](#nezapojení-dopravci)
- [Jak přidat dopravce](#jak-přidat-dopravce)

---

## Společná pravidla

**Jeden soubor = jeden dopravce.** Každý dopravce je třída
v `vyhledavac/providers/` označená `@PROVIDERS.register()`. Dostane
`SearchQuery` (odkud, kam, kdy, cestující) a vrátí seznam `Journey`
(úseky, časy, mezizastávky, ceny). Liší se jen tím, odkud data bere.

**Dva druhy dopravců:**

- **API** (`HttpProvider`) – posílají stejné JSON dotazy jako web dopravce.
  Jsou rychlé (1–5 s) a nepotřebují prohlížeč. Společný klient `http.py`
  řeší:
  - hlavičky jako skutečný prohlížeč a cookies;
  - minimální rozestup dotazů na jeden server (`min_interval`);
  - opakování s čekáním;
  - poznání blokace: HTTP 403, ochranná stránka Cloudflare/DataDome/Akamai
    místo JSON.
- **Prohlížečoví** (`BrowserProvider`, `needs_browser = True`) – ČD, ZSSK
  a OneTicket nemají použitelné API. Program ovládá skrytý Chromium přes
  Playwright (`browser.py`):
  - maskuje znaky automatizace;
  - nenačítá obrázky a reklamy;
  - pozná stránku s ochranou.

  Jsou pomalé (20–60 s) a citlivé na změny webu. Ve výchozím nastavení
  jsou vypnuté.

**Kdy se dopravce ptá.** Dopravci se zbytečně nevolají. Šetří to čas
a hlavně snižuje riziko, že dopravce IP adresu zablokuje.
- `international_only = True` – po Česku se dopravce vynechá úplně: ÖBB,
  ZSSK, PKP, MÁV, DB, NS, Trenitalia.
- Zahraniční dopravci mají navíc vlastní podmínku. Ptají se, jen když
  aspoň jeden konec cesty leží v jejich zemi (PKP – Polsko, MÁV – Maďarsko,
  NS – Nizozemsko, Trenitalia – Itálie, DB – Německo a země za ním). Země se
  pozná ze jména (`geo.country_of`) ještě před prvním dotazem.
- Odmítnutí se hlásí jako „nejezdí tam“ (`StationNotFoundError`), ne jako
  chyba.
- Travel hacky se na dílčí úseky ptají jen dopravců s `hack_legs = True`.
  IDS a DB jsou vypnuté: dostávaly by desítky dotazů.

**Cache.** Seznamy stanic, tokeny a vyřešená jména míst se ukládají do
SQLite cache (`cache.py`) s dobou platnosti. Seznam stanic MÁV
(2,5 MB) se tak stahuje jednou za 14 dní, ne při každém hledání.

**Chyba jednoho neshodí ostatní.** Engine spouští každého dopravce zvlášť
s časovým limitem (90 s). Chyba se ukáže u jeho řádku. Ostatní výsledky
zůstanou.

**Ceny a měny.** Každá cena se ukládá v měně dopravce (CZK, EUR, PLN, HUF)
a přepočítává se kurzem ČNB (`money.py`). Do výsledku jde jen cena za
**celou skupinu cestujících**. U dopravců, kteří umí jen jednoho
cestujícího (`multi_passenger = False`), na to upozorní žlutá poznámka.

**Doplnění cen.** Spoj bez ceny (např. z jízdního řádu ÖBB nebo NS)
dostane cenu od jiného dopravce, který stejný vlak prodává. Zařizuje to
filtr `price_backfill`: stejné vlaky a časy. U ceny je pak „Cenu prodává
X“.

**Cestující.** Z věku a ISIC se určí jedna společná kategorie
(`Passenger.category`): dítě do 6, dítě, mládež, student, dospělý, senior.
Každý dopravce ji přeloží na svůj kód (tabulka v README, sekce *Cestující
a slevy*). Noví zahraniční dopravci berou typ podle věku přímo z číselníku
dopravce.

---

## Přehled

| Dopravce | Soubor | Zdroj | Ptá se | Ceny | Stav (říjen 2026) |
|---|---|---|---|---|---|
| RegioJet | `regiojet.py` | JSON API | vždy | skupina, CZK | ověřeno živě |
| FlixBus | `flixbus.py` | JSON API | vždy | skupina, CZK | ověřeno živě |
| ČD | `cd.py` | prohlížeč (cd.cz) | ne na zastávky bez nádraží | skupina, CZK | ověřeno živě |
| OneTicket | `oneticket.py` | prohlížeč | jen Česko | skupina, CZK | ověřeno živě |
| Arriva | `arriva.py` | JSON API | jen po ČR | skupina, CZK | ověřeno živě |
| Bileto | `bileto.py` | JSON API | jen po ČR | skupina, CZK | ověřeno živě |
| Leo Express | `leo.py` | GraphQL API | jen jeho stanice | skupina, CZK | ověřeno živě |
| IDS | `ids_idos.py`, `idos.py` | IDOS + tarify IDS | jeden IDS / zastávka bez nádraží | skupina, z tarifu | ověřeno živě |
| Transitous | `transitous_jr.py`, `transitous.py` | JSON API (data CIS JŘ) | na přání; záloha IDS | bez cen | ověřeno živě |
| Plánovač | `planner.py`, `transitous.py` | JSON API Transitous (celá Evropa) | cesty mimo Česko / do ciziny | bez cen, ocení hack po kusech | ověřeno živě |
| ÖBB | `obb.py`, `_hafas.py`, `_oebb_shop.py` | HAFAS + e-shop API | ne po Česku | skupina, EUR | jízdní řád živě; e-shop z datacentra blokován |
| ZSSK | `zssk.py` | prohlížeč | ne po Česku | 1 osoba, EUR | ověřeno živě |
| PKP | `pkp.py` | Koleo API | konec v Polsku | 1 dospělý, PLN | ověřeno živě |
| MÁV | `mav.py` | JSON API e-shopu | konec v Maďarsku | skupina, EUR/HUF | ověřeno živě |
| DB | `db.py` | API int.bahn.de (+ prohlížeč) | konec v DE/NL/BE/FR/CH/DK/LU | skupina, EUR | stanice živě; spoje z datacentra blokovány |
| NS | `ns.py` | API ns.nl | konec v Nizozemsku | skupina, EUR (jen NL) | ověřeno živě |
| Trenitalia | `trenitalia.py` | API lefrecce.it | konec v Itálii | skupina, EUR (jen IT) | ověřeno živě |
| OUIGO | `ouigo.py` | JSON API (FR, ES) | konec ve FR/BE nebo ES | skupina, EUR | ověřeno živě |
| Renfe | `renfe.py` | formulář + DWR | konec ve Španělsku | dospělý → dopočet skupiny, EUR | ověřeno živě |
| Eurostar | `eurostar.py` | GraphQL | oba konce v síti Eurostaru | skupina, EUR | ověřeno živě |
| European Sleeper | `european_sleeper.py` | JSON API | obě stanice na jeho trase | skupina, EUR | ověřeno živě |
| CFR | `cfr.py` | HTML formuláře | konec v Rumunsku (jen vnitrostátně) | dospělý/dítě → skupina, RON | ověřeno živě |
| SŽ | `sz.py` | HTML formuláře e-shopu | jen po Slovinsku | skupina, EUR | ověřeno živě |
| HŽPP | `hzpp.py` | JSON API e-shopu | jen po Chorvatsku | skupina (děti jako dospělí), EUR | ověřeno živě |
| Vy | `vy.py` | JSON API vy.no | Norsko, Švédsko, Dánsko i mezi nimi | skupina, NOK | ověřeno živě |
| DSB | `dsb.py` | JSON API dsb.dk | konec v Dánsku/Švédsku, i do sousedních zemí (ceny hlavně vnitrostátní) | skupina, DKK | ověřeno živě |
| SJ | `sj.py` | JSON API sj.se | Švédsko, Norsko, Dánsko i do sousedních zemí | skupina, SEK | ověřeno živě |
| BDŽ | `bdz.py` | HTML jízdního řádu razpisanie.bdz.bg | jen po Bulharsku | z ceníku (skupina), EUR | ověřeno živě |
| CP | `cp.py` | JSON API api-gateway.cp.pt | jen po Portugalsku | dospělý z API, děti dopočítané, EUR | ověřeno živě |
| Srbijavoz | `srbijavoz.py` | JSON API eKarta | jen po Srbsku | skupina, RSD | ověřeno živě |
| SBB | `sbb.py` | opendata.ch + starý jízdní řád sbb.ch | konec ve Švýcarsku | z ceny s Halbtaxem ×2, CHF | ověřeno živě |
| SNCF | `sncf.py` | JSON API prodejce Kombo | konec ve Francii | skupina, EUR | ověřeno živě |
| Italo | – | – | – | – | nezapojeno (Akamai) |

---

## RegioJet

**Soubor:** `providers/regiojet.py` · **Typ:** JSON API (`brn-ybus-pubapi.sa.cz`) · **Klíč:** ne

**Postup:**
1. Při prvním hledání stáhne číselník měst a stanic (`/consts/locations`,
   ~145 kB). Uloží ho na 7 dní do cache.
2. Název z formuláře najde v číselníku, tolerantně k diakritice
   a překlepům, s aliasy („Vídeň“ = „Wien“).
3. Zavolá `/routes/search/simple` s kódem tarifu pro každého cestujícího.
4. U prvních spojů v časovém okně souběžně stáhne detail
   (`/routes/{id}/simple`). Z něj zná úseky, přestupy, mezizastávky
   a cenové třídy (Low cost, Standard, Business…).

**Cena:** nejlevnější volná třída pro celou skupinu, v CZK.

**Pozor:**
- Kódy tarifů klamou. `ISIC` je u RegioJetu senior 65+, student je
  `CZECH_STUDENT_PASS_26`.
- Vyprodaný spoj chodí s cenou 0 a `pricesCount: 0`. Program ho ukáže jako
  *Vyprodáno*, ne jako spoj zadarmo.

## FlixBus

**Soubor:** `providers/flixbus.py` · **Typ:** JSON API (`global.api.flixbus.com`) · **Klíč:** ne

**Postup:**
1. Přes našeptávač (`/search/autocomplete/cities`) najde ID měst.
2. Zavolá `/search/service/v4/search` se seznamem cestujících
   (`{"adult": 1, "student": 1}`). Datum chce ve tvaru `DD.MM.YYYY`.

**Cena:** včetně poplatku platformy (`total_with_platform_fee`), tedy to,
co skutečně zaplatíte. Za celou skupinu, v CZK.

**Pozor:**
- Studentská, seniorská a dětská sleva (50 %) platí jen na vnitrostátních
  linkách. Na mezinárodních FlixBus počítá plnou cenu sám.
- Když FlixBus slevovou kategorii na trase nezná (HTTP 400), program
  zopakuje dotaz pro dospělého a upozorní na to.
- Samotným dětem FlixBus jízdenku neprodá a API vrací nesmyslnou nulu.
  Taková cena se nezobrazí.
- Mezizastávky API nevrací, zná jen nástup a výstup.

## České dráhy

**Soubor:** `providers/cd.py` · **Typ:** prohlížeč (cd.cz) · **Klíč:** ne

ČD nemají veřejné API: formulář se odešle na server a ten vykreslí
stránku s výsledky.

**Postup:**
1. Otevře `cd.cz/spojeni-a-jizdenka/`, počká na doplnění formuláře
   JavaScriptem a zavře cookie lištu.
2. V okně *Cestující* nastaví tarif každého cestujícího, např. *Junior
   18–25 let, Žákovský průkaz ČR* pro studenta s ISIC. V souhrnu ověří, že
   se tarif opravdu změnil.
3. Vyplní odkud a kam přes našeptávač a odešle.
4. Na stránce `/spojeni-tam/…` přečte karty spojů. Z rozpisu karty
   (`li.detailed-schedule__item`) vezme každý vlak zvlášť: nástup, výstup,
   časy, číslo. Ikona autobusu znamená náhradní dopravu.

**Cena:** cena e-shopu za skupinu, v CZK.

**Pozor:**
- Formulář je na stránce třikrát (desktop, mobil, sbalený) a viditelný je
  jen jeden. Program bere první viditelnou kopii.
- Dialog „Čekejte…“ zachytává kliknutí, proto program čeká, až zmizí.
- Do října 2026 se z karty četl jen první odjezd a poslední příjezd. Spoj
  s přestupy ve Vídni pak vypadal jako jeden vlak. Opraveno, test
  `tests/data/cd_card_transfers.html.gz`.
- Web ČD hledá jen vlaky. Na autobusovou zastávku nebo obec bez nádraží
  („Domanín, Vrchy“) program ČD vůbec nevolá. Jinak by 90 s čekal na
  výsledky, které nepřijdou.
- Našeptávač zná zahraniční místa česky nebo bez cizí diakritiky (data CIS
  JŘ: „Gdaňsk“, „Gdansk Glowny“), ne „Gdańsk“. Program zkusí jméno, jak
  přišlo, pak české jméno z databáze měst, pak bez diakritiky; když
  našeptávač nenabídne nic, skončí hned („stanici web ČD nezná“). Dřív se
  bral jakýkoli viditelný seznam (i výběr „ODJEZD/PŘÍJEZD“) a hledání pak
  90 s čekalo na výsledky.
- Ze zemí, kde ČD nemá tarif (Švédsko), vrací spoje bez ceny. Hack
  `idos_complete` si to po první takové odpovědi zapamatuje a na tu zemi se
  přes prohlížeč dál neptá (každý dotaz ~20 s, jdou po jednom).
- Po mnoha dotazech z jedné IP vrací ČD stránku bez formuláře. Program to
  hlásí jako dočasnou blokaci.

## OneTicket

**Soubor:** `providers/oneticket.py` · **Typ:** prohlížeč · **Klíč:** ne
(jejich REST API je jen pro smluvní partnery)

Prodává jízdenky napříč českými železničními dopravci v jednotném tarifu
SJT. Je to proto dobrý zdroj cen českých vlaků.

**Postup:**
1. Otevře `oneticket.cz/search` a počká, až React vykreslí formulář
   (první 1–3 s je stránka prázdná).
2. Vyplní stanice a datum a nastaví cestující (Plné, Junior, Student,
   Senior).
3. Přečte JSON, který si stránka sama stáhla (`/search`, `/price`): vlaky,
   mezizastávky, dopravce. Čtení HTML zůstává jako záloha.

**Cena:** SJT pro skupinu, v CZK. Platí pro trasu, ne pro konkrétní vlak.

**Pozor:**
- Prodává jen jízdenky po Česku. Neznámou (zahraniční) stanici hlásí za pár
  sekund jako „nejezdí tam“.
- Když se stránka nevykreslí ani za 20 s, bývá to výpadek jejich API.

## Arriva

**Soubor:** `providers/arriva.py` · **Typ:** JSON API (`api.arriva.cz/v1`) · **Klíč:** anonymní token

**Postup:**
1. Anonymní token z cookie `ArrticCookie` (`shop.arriva.cz/api/trigger`).
2. `Place/Search?keyword=` najde místa. Obec Arriva nezná jako celek,
   proto se hledá nádraží i hlavní autobusová zastávka a výsledky se
   sloučí.
3. `Route/Search` vrátí spoje, `Price/CalculateBySearch` cenu.

**Prodává:**
- vlaky a expresní autobusy Arriva;
- jízdenky **SJT** i na vlaky jiných dopravců (ČD), dopravce je „jiný
  dopravce (SJT)“. Je to rychlý zdroj cen vlaků ČD bez prohlížeče.

**Cena:** `passengerPrice` je už celková cena za počet cestujících daného
typu. Nenásobit znovu (byla to chyba se dvojnásobnou cenou).

**Pozor:**
- Změna čísla vlaku (R 653 → R 96653) se ukazuje jako přestup. Bývá to
  náhradní doprava.
- Duplicitní autobusy (nádraží + zastávka) se slučují podle odjezdu
  a linky.
- Ptá se jen na cesty po Česku. Leží-li začátek nebo cíl prokazatelně
  v cizině (`geo.leaves_czechia`), končí bez dotazu na síť.

## Bileto

**Soubor:** `providers/bileto.py` · **Typ:** JSON API (`api.bileto.com/v1`) · **Klíč:** veřejné údaje z jejich JavaScriptu

Portál menších autobusových dopravců, kteří jinde online nejsou: RETROBUS
Line, Dinarica, UNITED BUSES a další.

**Postup:**
1. Token (client_credentials) – hlavička `Authorization` se posílá **bez**
   „Bearer“.
2. `places` – město nebo zastávka.
3. `connections` – spoje.
4. `POST /passengers` (věk, ISIC) a `POST /prices` – cena za celou
   skupinu.

**Pozor:**
- Seznam linek předem stáhnout nejde, proto se Bileto ptá na každou trasu.
  Neznámé místo = „nejezdí tam“.
- Dítě do 6 let jede zdarma. Studenti s ISIC, děti a senioři mají slevu.
- Ptá se jen na cesty po Česku. Leží-li začátek nebo cíl prokazatelně
  v cizině (`geo.leaves_czechia`), končí bez dotazu na síť.

## Leo Express

**Soubor:** `providers/leo.py` · **Typ:** GraphQL (`graph.leoexpress.com/le`) · **Klíč:** ne

**Postup:**
1. Dotaz `stations` – seznam ~190 stanic (cache 7 dní). Ptáme se jen,
   když jsou **obě** místa v tomto seznamu: Praha – Ostrava – Kraków /
   Košice.
2. Dotaz `searchResults` s cestujícími `[{name: tarif, cards: []}]`.
   Tarify: `child` 0–5, `child6` 6–17, `student` 18–25, `senior` 65+,
   jinak `adult`. Datum ve tvaru `dd.mm.YYYY`.

**Cena:** nejlevnější volná třída (Economy, Economy Plus, Business) pro
celou skupinu, v CZK.

**Pozor:**
- Bez hlavičky `Accept-Encoding: gzip, deflate` API vrací Brotli, které
  klient nemusí umět rozbalit.

## IDS (z IDOS)

**Soubory:** `providers/ids_idos.py`, `idos.py`, `ids/*` · **Typ:** HTML IDOS + tarify IDS · **Klíč:** ne

Web ČD neprodává jízdenky integrovaných systémů na autobusy. IDOS ale zná
všechny spoje včetně autobusů a MHD – i vlaky do ciziny (Domanín → Bad
Ischl přes České Budějovice, Linz a Selzthal) a dopravce každého úseku.
Cenu domácí části spočítá tarif systému (PID, IDESKA, IDS JMK, IDOL,
IREDO, DÚK, IDPK, VDV, IDZK, IDSOK, ODIS). IDS KK zapojený není.

**Postup:**
1. Výsledky `idos.cz/vlakyautobusymhdvse/spojeni/vysledky/?f=…&t=…`
   (nejvýš 2 stránky po 3 spojích). Úseky se dělí podle
   `div.outside-of-popup…`; pěší přesun („Přesun asi 8 min“,
   `div.walk`) je samostatný úsek „přesun“, ne další spoj.
2. Pro autobusy, vlaky ČD a vlaky přes státní hranici načte **detail
   spoje** (`/spojeni/draha/`, cache 24 h). Získá z něj:
   - zastávky, kde vozidlo opravdu staví, s časy a kilometry (tarif ODIS);
   - systém linky („linka 300 [IDESKA]“), jinak seznam linek z JDF
     (`data/ids_lines.json.gz`, obnova
     `python tools/build_ids_lines.py --jdf JDF.zip`).
3. Zóny zastávek z databáze `data/stations.json.gz`. Stejnojmenné obce
   (Domanín u Třeboně × Domanín na Moravě) rozliší podle systému sousedních
   zastávek.
4. **Plánovač jízdenek** (dynamické programování) najde nejlevnější pokrytí
   jízdenkami IDS. Vlak smí rozdělit jen ve stanici, kde staví, i mezi
   dvěma systémy v přesahu (IDESKA do Benešova + PID do Prahy). Úseky mimo
   IDS (dálkový autobus, EC, vlaky v cizině) zůstanou k dokoupení. Hack
   `idos_complete` se na ně zeptá dopravců a vybere nejlevnější rozpis na
   **tentýž spoj**: po Česku Arriva/SJT, Bileto, RegioJet, FlixBus, Leo
   Express (a ČD, OneTicket přes prohlížeč); do ciziny dopravci, kteří
   obsluhují obě země (ÖBB, DB, PKP, MÁV, RegioJet, FlixBus…) a přes
   prohlížeč ČD (z Česka kamkoli – Včasná jízdenka Evropa) a ZSSK. Do ciziny
   se ptá na město („České Budějovice, aut.nádr.“ → České Budějovice).
   Kus do ciziny ocení celý **i** rozdělený – nejdřív co nejdelší začátek
   (ČD co nejdál), zbytek znovu – v přestupu nebo ve stanici na hranici;
   porovná dva nejdelší lomy s celou jízdenkou a vezme nejlevnější. Když
   konkrétní vlak za lomem nikdo neprodá (WESTbahn), vezme nejbližší
   navazující spoj prodejce. Spoje z IDOS se oceňují postupně; s ČD přes
   prohlížeč to i tak trvá několik minut.
   - Domanín → Bad Ischl = IDESKA do Českých Budějovic + ČD/ÖBB do Bad
     Ischlu, jinak ČB → Linz/Selzthal + zbytek ÖBB;
   - Třeboň → Lublaň = IDESKA do ČB + ČD do Villachu + ÖBB do Lublaně
     (případně ÖBB do Jesenic + SŽ); ověřeno: 49 + 411 + 193 = 653 Kč;
   - Brno → Malacky = ČD/ZSSK celé, jinak Brno → Kúty (ČD) + Kúty →
     Malacky (ZSSK).

   Země stanice se určuje podle trasy: „Jesenice“ mezi Villachem a Lublaní
   je slovinská, „Rybník“ ve vlaku z Budějovic česká (ne polský Rybnik).

   **Navazující varianty:** ke spoji z IDOS nabídne i jiné spoje na zbytek
   cesty z odpovědí prodejců, které navazují na jízdenku IDS (rezerva
   4 min ve stejné stanici, 10 min jinde ve městě, čekání nejvýš 3 h) –
   nejlevnější a nejdřív přijíždějící. Trhové Sviny → Salzburg: IDS do
   Budějovic a FlixBus podle IDOS, nebo vlak ČD/ÖBB – porovnáte cenu,
   přestupy i čas.

**Kdy se ptá:** vždy, když obě místa známe – i na cesty po cizině. IDOS
zná vlaky celé Evropy, FlixBus i RegioJet (Stockholm → Amsterdam,
Bukurešť → Graz, Vídeň → Mnichov). Zahraniční město se do IDOS zadá
jednoznačně podle jeho našeptávače (`Ajax/SearchTimetableObjects`,
JSONP): „Košice; Slovensko“, „Kraków; Polsko“ – samotné „Košice“ IDOS
odmítne jako nejednoznačné. Kde IDOS spojení nenajde (Valencie → Turín,
Kraków → Hamar), zbývá plánovač Transitous. Ocení se první 3 spoje do
4:15 h.

**Jízdenka z e-shopu IDOS:** IDOS ke spoji nabídne jízdenku
(`Ajax/GetPriceOffer` s `handle` a `connId` z výsledků) – prodává ji ČD,
i na cesty do ciziny – a řekne, na které úseky platí
(`sellablePartsDesc`): Domanín → Bad Ischl 759 Kč na „Borovany - Bad
Ischl“, Třeboň → Lublaň 1 643 Kč celé, Košice → Bordeaux 2 492 Kč na
„Košice - Praha hl.n.“ a „Praha hl.n. - Offenburg“. Rozpis „jízdenka
z IDOS + IDS na zbytek (+ dokoupit, co nepokryje)“ se porovná s ostatními.
Zatím jen pro jednoho dospělého bez slev (cena z IDOS je pro dospělého).
Jen když se každá část jízdenky dotýká Česka – kus celý v cizině
(„Salzburg Hbf - Ljubljana“, Kodaň → Amsterdam) ocení dopravci té země;
spoje IDOS vrací spolehlivě, jeho ceny v cizině ověřené nemáme.

**ODIS** (Moravskoslezský kraj) má kilometrický tarif: jednotlivá
elektronická jízdenka REGION 10 Kč + 1,50 Kč/km, zvýhodněná (děti 6–15,
15–18, studenti do 26, 65+) 5 Kč + 0,75 Kč/km, na desetihaléře
([odis.cz](https://www.odis.cz/zakladni-informace-o-jizdnem),
[kodis.cz](https://www.kodis.cz/plne-jednotlive-jizdne)). Kilometry bere
z detailu spoje v IDOS. Platí v regionálních autobusech a ve vlacích ČD,
GW Train Regio, MBM rail a RegioJet (R8). Opava východ → Ostrava hl. n.
(33 km) = 59,50 Kč. Žádná ochrana se neobchází – cena je z veřejného
tarifu, ne z e-shopu ODISka.

**Pozor:**
- Platnost na vlacích:
  - jízdenky IDS platí na Os/Sp/R ČD;
  - **IDESKA** platí ve 2. třídě i na Ex/IC (Jižní expres) na tratích
    190/191, 194, 196, 220 (až do Benešova), 225 a 226;
  - na EC a railjety neplatí žádné IDS.
- Cena je z tarifu, ne od prodejce. Jízdenku koupíte v aplikaci systému,
  u řidiče nebo v automatu.
- Je-li IDS cena vlakem ČD od 80 Kč na osobu, zkusí se i jízdenka ČD.
  Levnější kombinace spoj z IDS nahradí.
- **Dopravce:** IDOS u každého úseku uvádí provozovatele (`span.owner` –
  „ČSAD AUTOBUSY České Budějovice a.s.“, „České dráhy, a.s.“), program ho
  ukazuje u úseku.
- **Citlivost IDOS na počet dotazů** (měřeno 12. 10. 2026 z datacentra):
  40 hledání po sobě bez pauzy za 17 s prošlo všech 40 (HTTP 200, žádná
  captcha). Limit jsme nenarazili; program se přesto ptá s rozestupem
  0,7 s, nejvýš 2 stránky výsledků a detaily spojů drží 24 h v cache.
- **Záloha Transitous:** když IDOS odpoví blokací, limitem (429) nebo
  chybou, vezmou se spoje z [Transitous](#transitous--jízdní-řády-bez-cen)
  (stejná data CIS JŘ). Zastávky a licence linek jsou už ve spoji, detail
  z IDOS se nestahuje; spoj nese poznámku „Spoj z Transitous“.

## Plánovač – trasy po celé Evropě

**Soubory:** `providers/planner.py`, `transitous.py` · **Typ:** JSON API Transitous · **Klíč:** ne

IDOS zná jen cesty z Česka a do Česka. Cesty jinde (Kraków → Hamar,
Stockholm → Amsterdam, Valencie → Turín) naplánuje Transitous
(`api.transitous.org/api/v5/plan`, MOTIS nad otevřenými jízdními řády
Evropy) – vlaky, FlixBus i MHD s dopravcem každého úseku, bez cen.

1. Zahraniční město → souřadnice středu z databáze měst (GeoNames), české
   místo → zastávka podle jména. (Geokódování podle jména vracelo i cizí
   data: Kraków ze švédského feedu, Paříž v Brazílii.)
2. MHD a chůze na začátku a konci trasy se vypustí, spoj začíná a končí na
   nádraží. MHD uprostřed (metro mezi pařížskými nádražími, S-Bahn po
   Berlíně, kratší než 12 km) zůstane jako úsek „MHD“ bez ceny.
3. První 3 spoje do 4:15 h vrátí bez ceny s rozpisem „koupit celé“ a hack
   `idos_complete` je ocení po kusech (viz IDS výše): prodejce musí
   obsluhovat všechny země kusu, FlixBus jen autobusy, ČD přes prohlížeč
   jen vlaky; dělí se v přestupech a na hranicích uvnitř vlaku, nejvýš
   40 dotazů na spoj (z toho 6 přes prohlížeč). Co nikdo neprodá, ukáže
   se jako spoj s částí cen („◐“) a seznamem úseků, kde cena chybí.

Kdo smí prodat kus cesty:
- dopravce musí obsluhovat všechny země kusu (`countries`) a dopravci
  s domácí zemí (`home` – MÁV, PKP, NS, SNCF, Trenitalia, Renfe, DSB …)
  jen kus, který v ní začíná nebo končí (MÁV neprodá Záhřeb → Lublaň);
- ČD přes prohlížeč i vlak, který Českem jen projíždí (EC Krakov → Vídeň);
- prodejce, kterého hack chce, se použije i tehdy, když není v seznamu
  zapnutých dopravců (ČD přes prohlížeč ve výchozím výběru není) – jen
  pro dotazy hacku (`SearchEngine._helpers_for`).

Hranice uvnitř vlaku se hledá podle zemí zastávek; „ostrůvek“ až tří
zastávek jiné země mezi dvěma zastávkami téže země je jmenovec odjinud
(slovinská „Sava“ není italská, „Blanca“ španělská) a lom tam nevznikne.
Navazující jízdenka se počítá se skutečným časem přestupu (`transfer.py`,
viz README) – i za lomem, kde jiný spoj přijede jinam nebo později.

Čistě vlakové hledání (`--trains`, `SearchQuery.modes`): IDOS se ptá na
kombinaci „vlaky“, plánovač s `transitModes=RAIL`, ostatní dopravci se
odfiltrují na spoje jen vlakem.

Ověření 13. 10. 2026 na cesty 15. 10. od 6:00 (IDOS + plánovač + 20
dopravců, `--trace`):

| Cesta | Nejlepší nalezené jízdenky | Doba hledání |
|---|---|---|
| Kraków → Hamar | FlixBus do Berlína 848 + ÖBB Berlín → Hamburk 1 512 + FlixBus do Osla + Vy do Hamaru = 4 900 Kč | 2 min |
| Stockholm → Amsterdam | SJ do Lundu 1 492 + SJ do Kodaně 420 + IDOS Kodaň → Amsterdam 6 247 = 8 159 Kč | 3 min |
| Košice → Bordeaux | FlixBus 3 696 Kč; RegioJet + FlixBus 4 035 Kč; RegioJet + FlixBus + SNCF 4 884 Kč | 12 min |
| Bukurešť → Graz | FlixBus 2 107 Kč; FlixBus do Vídně + ÖBB 2 001 Kč | 2,5 min |
| Třeboň → Paříž | ČD do Prahy + FlixBus 2 413 Kč; IDOS do Frankfurtu + SNCF 5 215 Kč | 12 min |
| Valencie → Turín | Renfe do Barcelony + FlixBus přes Lyon 2 462 Kč | 2 min |

Stres testy čistě vlakem (`--trains --hacks idos_complete --trace`,
13. 10. 2026 na cesty 15. 10.), města nad 10 tis. obyvatel, trasa přes
jednu až dvě třetí země. Každý pár se opakoval, dokud nevyšel:

| Cesta (přes) | Co bylo potřeba opravit | Nejlevnější nalezené jízdenky | Doba |
|---|---|---|---|
| Rijeka → Olomouc (SI, AT) | falešné „hranice“ (Sava → Itálie, Blanca → Španělsko, „Loka(SZ)“), MÁV ptaný mimo Maďarsko, ÖBB neprodával z Chorvatska | HŽPP do Savského Marofu 236 + ÖBB do Villachu 329 + ÖBB do Olomouce 895 = 1 460 Kč | 6 min |
| Gdańsk → Budapešť (CZ, SK) | ČD nebyly zapnuté a neznaly „Gdańsk“ (90 s timeout), navazující spoj nestihl odjezd dalšího kusu, ÖBB neprodával z Polska | ÖBB do Vídně 778 + MÁV 585 = 1 363 Kč (4 ze 4 spojů s cenou) | 7 min |
| Malmö → Vídeň (DK, DE, CZ) | ČD ze Švédska ceny nedává (učení), ÖBB neprodával z Dánska, cache brala spoj bez ceny jako vyřízený | SJ do Kodaně 348 + ČD do Břeclavi 2 490 + ÖBB 239 = 3 078 Kč; noční vlak 7 141 Kč | 7 min |
| Amsterdam → Bratislava (DE, AT) | prošlo napoprvé | ÖBB do Salcburku 5 927 + ÖBB 1 910 = 7 837 Kč (DB blokovaná) | 5 min |

Další kolo (13. 10. 2026, `--trains`, všechny výchozí hacky):

| Cesta (přes) | Co bylo potřeba opravit | Výsledek | Doba |
|---|---|---|---|
| Amsterdam → Bratislava (DE, CZ) 11. 11. | European Sleeper se do kombinací nedostal (nový hack `night_train`) | ES 453 do Prahy 1 219 + RegioJet 429 = **1 648 Kč** (ÖBB 3 295 Kč) | 3,7 min (dřív 7,8) |
| Bukurešť → Bratislava (HU, AT) | IDOS, MÁV i HAFAS (ÖBB) braly rumunský místní čas jako český (o hodinu vedle); spoje víc než 4 h po zadaném čase se vůbec neoceňovaly | MÁV do Hegyeshalomu 1 234 + ÖBB 132 + 176 = **1 541 Kč**; MÁV přes Szob 1 627 Kč | 2,9 min |
| Sofie → Segedín (RO) | „Segedín“ databáze neznala, Kombo (SNCF) vracelo letenky Turkish Airlines jako vlak, cyrilice („София“) se ztrácela, vlak ve 22:00 vypadl z okna 12 h | jen částečně: BDŽ do Ruse 274 + CFR Giurgiu → Bukurešť 244 + MÁV do Békéscsaby 959 = 1 477 Kč; **Ruse → Giurgiu online nikdo neprodává** (mezinárodní e-shop CFR chrání Cloudflare Turnstile) | 1 min |

Úseky v Německu prodává hlavně DB – ze serveru, kde se zkoušelo, je DB
blokovaná (Akamai), proto tam pomáhaly ÖBB, FlixBus a IDOS. Nejdéle trvají
dotazy na ČD a ZSSK přes prohlížeč.

## Transitous – jízdní řády bez cen

**Soubory:** `providers/transitous_jr.py`, `transitous.py` · **Typ:** JSON API api.transitous.org · **Klíč:** ne

Otevřený evropský plánovač (MOTIS) nad veřejnými jízdními řády. Pro Česko
má celostátní data CIS JŘ (všechny autobusy z JDF, vlaky z CZPTT) a data
IDS – tedy totéž co IDOS. Ceny nezná.

**Postup:**
1. `GET /api/v1/geocode?text=…&language=cs` – zastávka se stejným jménem
   (`cz-JDF-merged_jdf:stop:33720` = „Domanín,Vrchy“), jinak obec jako
   souřadnice.
2. `GET /api/v5/plan?fromPlace&toPlace&time` – spojení, `nextPageCursor`
   pro další stránku. Úsek má dopravce (`agencyName`), linku
   (`routeShortName`), licenci a spoj (`tripShortName` „340100 7“, u vlaku
   „R 865“) a mezizastávky s časy (UTC).

**Kdy se ptá:** samostatný dopravce „Transitous“ jen na přání (ve výchozím
výběru není). IDS ho použije sám jako zálohu, když IDOS nejde.

**Pozor:**
- Data jsou sloučená z více zdrojů – stejný vlak přijde z CZPTT, ZSSK
  i KORDIS. Spojení se slučují podle odjezdu, příjezdu a první linky.
- Pro obec (Třeboň) začíná spojení na nejbližších zastávkách, ne vždy na
  té, kterou zvolí IDOS (06:30 Táboritská × 06:28 aut. nádr.).
- Komunitní služba – posíláme User-Agent s odkazem na projekt a ptáme se
  střídmě.

---

## ÖBB

**Soubory:** `providers/obb.py`, `_hafas.py`, `_oebb_shop.py` · **Typ:** dvě API · **Klíč:** anonymní token e-shopu

Dva nezávislé zdroje, které běží souběžně:
- **Jízdní řád** z HAFASu (`fahrplan.oebb.at/bin/mgate.exe`). Vrací spoje
  se všemi mezizastávkami po celé Evropě, i české vlaky. Na tom stojí hack
  „lomená jízdenka“.
- **Ceny** z e-shopu `shop.oebbtickets.at`: anonymní token, pak `init` →
  `stations` → `travelActions` → `timetable` → `prices`. Ceny se ke spojům
  přiřadí podle času odjezdu a příjezdu.

**Pozor:**
- **Cloudflare odmítá TLS otisk Pythonu.** E-shop se proto volá přes
  `curl_cffi` s otiskem Chromu. Když to nejde, zkusí se dotaz uvnitř
  prohlížeče.
- E-shop blokuje IP adresy serveroven a VPN. Ceny ÖBB šlo při vývoji
  ověřit jen na nahraných odpovědích. Z domácí sítě obvykle projde.
- **Cena jen za část trasy.** E-shop občas prodá jízdenku jen na úsek
  (`reducedScope`, např. Třeboň → Marchegg za 14,90 €, ale ne do
  Bratislavy). Taková cena se u spoje neukáže, uloží se do
  `raw["partial"]` a hack `obb_partial` dokoupí zbytek jinde.
- Slevy (Vorteilscard, děti) počítá ÖBB až v košíku. Program ukáže cenu za
  dospělého a upozorní na to.

**Co uvidíte v různých situacích:**

| Situace | Výsledek |
|---|---|
| Všechno funguje | spoje ÖBB s cenami, mezizastávkami a 1. třídou |
| E-shop odmítne přímý dotaz | zkusí se totéž zevnitř prohlížeče (je-li Playwright) |
| E-shop odmítne i prohlížeč, nebo je pomalý (> 45 s) | spoje bez ceny + žluté upozornění; ceny doplní jiní dopravci stejného vlaku |
| Spadne jízdní řád, e-shop odpoví | spoje z e-shopu s cenou, bez mezizastávek |
| Spadne obojí | chyba u ÖBB, ostatní dopravci běží dál |

**Chyby, které jsme už opravili** (ať se nevrátí):
- e-shop přestal posílat `availabilityState` a program kvůli tomu
  zahazoval každou cenu;
- prázdné `"datetimeArrival": ""` přimělo e-shop ignorovat datum a vracet
  spoje „od teď“, které se pak nespárovaly s jízdním řádem;
- `init` se zkoušel ve verzích v4 → v5 → v6 a skutečnou chybu z v4
  přepsala 404 z neexistující v6.

## ZSSK

**Soubor:** `providers/zssk.py` · **Typ:** prohlížeč (`predaj.zssk.sk`, JSF) · **Klíč:** ne

**Postup:**
1. Vyplní stanice. Návrhy našeptávače čte z `/autocompleteStations/…`
   a vybere ten, který opravdu odpovídá („Vídeň“ ≠ Vígľaš).
2. Datum ve tvaru `30. 9. 2026`.
3. Přečte seznam spojů po vlacích. Jízdní řád pošle hned (~5 s).
4. Cenu zná až nákupní formulář. Pro prvních 10 spojů projde *Kúpiť
   lístok → Voľba cestujúcich → Voľba cestovného lístka*, souběžně ve 3
   oddělených kontextech prohlížeče. Každou cenu zobrazí hned.

**Pozor:**
- Slovenská 100% sleva (studenti, senioři se slovenským nárokem) se zapne
  kartou `sk_free`. Cena 0 € je pak platná.
- Spoj, u kterého formulář nepokračuje (lůžkový vůz, trasa mimo e-shop),
  je „nelze koupit online“.
- Po Česku se ZSSK neptá.

## PKP (Koleo)

**Soubor:** `providers/pkp.py` · **Typ:** JSON API Koleo · **Klíč:** ne
(hlavičky `X-KOLEO-Version`, `X-KOLEO-Client`)

**Postup:**
1. `koleo.pl/ls?q=` – našeptávač. Skupina „praha“ znamená celé město.
2. `/api/v2/main/stations` – země stanic (cache 30 dní). Pokud ani jeden
   konec není v Polsku, dál se neptá.
3. `/api/v2/main/connections` – spoje se zastávkami, dopravce podle
   `/brands` (PKP Intercity, Polregio, Koleje Śląskie…).
4. `/connections/{id}/price` – cena, nejvýš pro 10 spojů.

**Cena:** normální jízdné **za jednoho dospělého**, v PLN. Polské slevy
platí jen pro polské nároky. U skupiny na to upozorní poznámka.

## MÁV

**Soubor:** `providers/mav.py` · **Typ:** JSON API e-shopu (`jegy-a.mav.hu/IK_API_PROD/api/OfferRequestApi`) · **Klíč:** ne (hlavička `UserSessionId` = libovolné UUID)

**Postup:**
1. `GetStationList` – ~54 tis. míst (cache 14 dní). Zahraniční stanice
   jsou bez diakritiky („Brno hl. n.“). „Budapest“ = skupina
   `BUDAPEST*`, tedy všechna nádraží.
2. `GetCustomersAndDiscounts` (`OfferKind` = `InternalTicket` /
   `InternationalTicket`) – typy cestujících podle věku. Klíče se mění,
   proto se čtou z číselníku.
3. `GetServicesAndSearchServices` – kód služby „přeprava ve 2. třídě“
   (vnitrostátně 52, mezinárodně 50).
4. `GetOfferRequest` – `offerkind` „1“ po Maďarsku, „4“ mezinárodně.
   Vrací spoje s rozpisem vlaků (`details.routes`).

**Cena:** `travelClasses` třídy „2“ = celá skupina. Mezinárodně v EUR, po
Maďarsku v HUF.

**Pozor:**
- Bez `UserSessionId` vrací API 400. Se starým typem cestujícího hlásí
  „Ismeretlen utas típus“.
- Pro věk, na který MÁV nemá typ (mezinárodně např. 5 let), se nehledá.
- Ptá se jen, když aspoň jeden konec leží v Maďarsku. Jinak se nestáhne
  ani seznam stanic.

## DB – Deutsche Bahn

**Soubor:** `providers/db.py` · **Typ:** JSON API `int.bahn.de/web/api` (jako db-vendo-client, profil „dbweb“) · **Klíč:** ne

**Postup:**
1. `GET reiseloesung/orte?suchbegriff=…&typ=ALL` – stanice. `id` je
   řetězec HAFAS („A=1@O=Praha hl.n.@…@L=5400014@“), posílá se dál beze
   změny.
2. `POST angebote/fahrplan` – spoje s úseky (`verbindungsAbschnitte`),
   zastávkami (`halte`) a nejlevnější cenou (`angebotsPreis`).
   Cestující: `reisende` s typem KLEINKIND (0–5), KIND (6–14),
   JUGENDLICHER (15–26), ERWACHSENER, SENIOR (65+) a věkem.

**Cena:** Sparpreis/Flexpreis 2. třídy bez BahnCard, za skupinu, v EUR.

**Pozor:**
- **Ochrana Akamai.** Z IP adres datových center vrací API
  `OPS_BLOCKED` (HTTP 403). Platí i pro dotazy bez cookie `_abck`, kterou
  nastaví JavaScript webu.
  - Program nejdřív pošle obyčejný HTTP dotaz. Z domácího připojení
    obvykle projde.
  - Při 403 zkusí totéž `fetch()` uvnitř stránky int.bahn.de
    v prohlížeči, pokud je nainstalovaný Playwright.
  - Při vývoji z datacentra prošlo vyhledání stanic, dotaz na spoje ne.
    Zpracování odpovědi je podle formátu db-vendo-client, **živě
    neověřené**. Když DB selže, ukáže se to v *Co se nepovedlo*.
- Ptá se, když aspoň jeden konec leží v DE, NL, BE, FR, CH, DK nebo LU.
  Do dílčích úseků hacků se DB neptá (pomalé a blokované).
- Jízdní řád DB po Evropě umí i ÖBB (HAFAS), takže spoje přes Německo
  uvidíte i bez DB, jen bez cen DB.

## NS – Nizozemské dráhy

**Soubor:** `providers/ns.py` · **Typ:** JSON API `gateway.apiportal.ns.nl` · **Klíč:** `Ocp-Apim-Subscription-Key` ze stránky ns.nl

**Klíč API.** Plánovač na `www.ns.nl/reisplanner/` vypisuje klíč
v `window.journeyplannerConfiguration.nsAppInternalProductKey`. Program
ho čte ze stránky (cache 1 den), takže když ho NS vymění, vezme si nový.
Pozor, na stránce je i jiný klíč (`suggestHeaders`, jen pro vyhledávání
na webu), který pro spoje neplatí (HTTP 401).

**Postup:**
1. `nsapp-stations/v3?q=` – stanice, i zahraniční („Berlin Hbf“, země
   `D`). Kód stanice `id.code` (ASD, UT, BERHBL).
2. `reisinformatie-api/api/v3/trips?fromStation&toStation&dateTime` –
   spoje s mezizastávkami (průjezdy `passing` se vynechají).

**Cena:** `productFare` = jednotlivá jízdenka 2. třídy pro **jednoho
dospělého** bez slevy. Pro skupinu se sčítá:
- od 12 let plná cena;
- děti 4–11 let s dospělým jízdenka Railrunner (2,50 €);
- do 4 let zdarma.

**Pozor:**
- **Cena je jen po Nizozemsku.** U mezinárodních spojů (Amsterdam →
  Berlín) NS vrátí jízdní řád bez ceny. Cenu doplní jiný dopravce (DB,
  FlixBus), nebo zůstane „cena neznámá“ s poznámkou o NS International.

## Trenitalia

**Soubor:** `providers/trenitalia.py` · **Typ:** JSON API `www.lefrecce.it/Channels.Website.BFF.WEB/website` · **Klíč:** ne

**Postup:**
1. `GET locations/search?name=` – stanice. ID začíná kódem země UIC (`83…`
   Itálie, `81…` Rakousko). „Venezia ( Tutte Le Stazioni )“ = všechna
   nádraží města.
2. `POST ticket/solutions` – spoje (`nodes` = vlaky) s cenou nejlevnější
   nabídky pro `adults` (od 15 let) + `children` (4–14 let; do 4 let
   zdarma, nepočítají se).

**Cena:** po Itálii nejlevnější nabídka za skupinu, v EUR.

**Pozor:**
- **Mezinárodní ceny se nepoužívají.** U spojů do zahraničí vrací
  Trenitalia buď `indicative: true` (orientační cena), nebo cenu **jen
  za italskou část**. Benátky → Vídeň za 36,15 € pro 3 osoby = jen
  Benátky → Udine. Program takovou cenu u spoje neukáže, nechá jízdní
  řád a poznámku s orientační cenou.
- Slevové karty (CartaFreccia) se neuplatňují.
- Ptá se jen, když aspoň jeden konec leží v Itálii.

## OUIGO

**Soubor:** `providers/ouigo.py` · **Typ:** JSON API `mdw.api-fr.ouigo.com/api` (Francie) a `mdw02.api-es.ouigo.com/api` (Španělsko) · **Klíč:** JWT z `Token/login` s údaji webové aplikace (stojí v JavaScriptu e-shopu, není to uživatelský účet)

**Postup:**
1. `POST Token/login` → token na 2–3 h (cache do vypršení). Dál hlavičky
   `Authorization: Bearer …` a `Content-Language: fr-FR` / `es-ES`.
2. `GET Data/GetStations` – stanice se skupinami měst („PT1“ Paris toutes
   gares, „LY1“, „MT1“ Madrid – Todas) a `connected_stations`. Bez spojení
   se hledání neposílá. Cache 7 dní.
3. `POST Sale/journeysearch` – `{"outbound_date", "origin", "destination",
   "passengers": [{"type": "A"}, …]}`. Vrací spoje celého dne, i
   s přestupem (Barcelona → Sevilla přes Madrid).

**Cena:** `price` = nejlevnější nabídka (OUIGO Essentiel/Esencial) **za celou
skupinu**:
- Francie: `A` od 12 let, `C` dítě do 11 let (8 €), do 4 let „Toupti“ bez
  místa zdarma;
- Španělsko: `A` od 14 let, `C` 4–13 let (7 €), `B` 0–3 roky (0 €).

`full: true` = vyprodáno.

**Pozor:**
- Francie (+ Brusel) a Španělsko jsou oddělené sítě. Mezi nimi OUIGO
  nejezdí, na Madrid – Paříž se neptá.
- Údaje aplikace se mohou s novou verzí webu změnit. Najdete je v bundlu
  `/assets/index-*.js` (ventas) nebo `core-chunk-*.js` (ventes).
- Cloudflare Turnstile chrání jen nákup, hledání ho nepotřebuje.
- API vrací celý den, časové okno filtruje program.

## Renfe – Španělské dráhy

**Soubor:** `providers/renfe.py` · **Typ:** formulář + DWR (Java RPC) na `venta.renfe.com/vol` · **Klíč:** žádný, jen session cookie

**Postup:**
1. `estacionesEstaticas.js` z renfe.com – statický seznam ~1 400 stanic
   (kódování ISO-8859-1, cache 7 dní).
   - Kód `cdgoEstacion` je číslo stanice, nebo skupina města („MADRI“ =
     MADRID (TODAS)).
   - `cdgoAdmon`: 0071 = Španělsko, 0087 = Francie, 0094 = Portugalsko.
2. `POST buscarTren.do` – formulář hledání. Datum `dd/mm/rrrr`,
   `adultos_` 14+, `ninos_` 4–13, `ninosMenores` 0–3. Hledání se uloží do
   serverové session.
3. `dwr/call/plaincall/__System.generateId.dwr` – ID DWR session.
4. `dwr/call/plaincall/trainEnlacesManager.getTrainsList.dwr` – spoje
   celého dne včetně přestupů, cena `tarifaMinima` a tarify (Básico,
   Elige, Prémium…). Odpověď je JavaScript, převádí ji `parse_dwr`.

**Cena:** API vrací cenu **za jednoho dospělého**. Při 4 a více osobách už
v ní je skupinový tarif „4 o más“. Pro skupinu se dopočítává podle
pravidel Renfe:
- od 14 let plná cena;
- děti 4–13 let −40 %;
- AVLO: dítě pevně 5 €;
- do 4 let zdarma.

U přestupu AVE + AVLO s dětmi je cena jen orientační: uloží se do `raw`
s poznámkou, jako cena spoje se nebere.

**Pozor:**
- **DWR ignoruje parametry** a vrací spoje posledního hledání ze session.
  Každé hledání je proto POST formuláře + DWR dotaz a souběžná hledání
  řadí zámek.
- Prodává jen vlaky Renfe (ne OUIGO ani iryo). Do Francie jen AVE
  Internacional (Barcelona – Lyon, Madrid – Marseille), do Paříže ne.
- Dětské slevy jsou dopočtené z pravidel Renfe, ne z API (neověřeno
  nákupem).
- Hledá se jen den odjezdu, okno přes půlnoc nepokračuje.
- Web chystá nový prodejní systém, pak se postup změní.

## Eurostar

**Soubor:** `providers/eurostar.py` · **Typ:** GraphQL `site-api.eurostar.com/gateway` · **Klíč:** žádný

Eurostar od roku 2023 zahrnuje i bývalý Thalys. Jezdí do Londýna, Paříže,
Lille, Bruselu, Amsterdamu, Rotterdamu, Kolína nad Rýnem, Cách,
Düsseldorfu, Essenu a Dortmundu. S přípoji SNCF/SNCB prodává i Lyon,
Antverpy, Ženevu a další.

**Postup:**
1. `query getStations` – ~240 prodávaných stanic (cache 7 dní): `uic`,
   anglický název, město, země.
   - Zadání se hledá podle názvu, pak podle města. České názvy (Londýn,
     Paříž, Brusel, Kolín nad Rýnem) převádí tabulka aliasů.
   - U města má přednost stanice Eurostaru (Paříž = Gare du Nord).
2. `query NewBookingSearch` → `journeySearch(outboundDate, origin,
   destination, adults, youths, children, seniors, infants, currency: EUR)`
   – spoje celého dne s úseky a nabídkami po třídách.

**Cena:** `fares[].prices.total` = cena pro **celou skupinu** v dané třídě
(Standard, Plus, Premier), v EUR. Věkové skupiny:
- do 3 let zdarma bez místa;
- 4–11 let děti, 12–25 mládež, 26–59 dospělí, od 60 senioři.

Vyprodaná třída má `prices: null`.

**Pozor:**
- **Časy jsou místní** (Londýn v britském čase) a `isoDate` se „Z“ není
  UTC. Časové pásmo se určuje podle UIC kódu.
- API vrací celý den bez ohledu na hodinu. Okno filtruje program a přes
  půlnoc se zeptá i na další den.
- Ceny se berou v EUR: v GBP API u některých přípojů cenu nevrátí.
- Spojené soupravy chodí dvakrát s jiným číslem (9339/9439).
- Ptá se jen, když oba konce leží v síti Eurostaru (GB, FR, BE, NL,
  Porýní). Praha → Paříž se přeskočí bez dotazu.

## European Sleeper

**Soubor:** `providers/european_sleeper.py` · **Typ:** JSON API `europeansleeperprod-api.azurewebsites.net/api` · **Klíč:** ne

E-shop `booking.europeansleeper.eu` je vlastní (Next.js), ne Bileto.
Stanice a trasy jsou v modulu staticky (ověřeno z `GET /api/constants`,
říjen 2026):
- Praha – Ústí – Děčín – Drážďany – Berlín – Amersfoort – Amsterdam –
  Rotterdam – Antverpy – Brusel;
- Brusel – Kolín – Curych – Milán;
- Paříž – Brusel – Berlín.

Když obě stanice neleží na stejné trase, program se vůbec neptá.

**Postup:**
1. `POST /api/search/trains` – jízdy na několik dní kolem data. V den, kdy
   vlak nejede, je `trip: null`. Praha → Amsterdam jede (říjen 2026) v úterý,
   čtvrtek a neděli v 18:00.
2. `POST /api/search/availability` – kategorie míst (sedadlo, lehátko,
   lůžko, celé kupé) a tarify Easy/Good/Flex Night.

**Cena:** za **celou skupinu** v EUR. Dospělý od 12 let, dítě 4–11 let,
do 3 let zdarma bez vlastního místa. Cena spoje je nejlevnější prodejná
kategorie a v popisku je, která (např. „lehátko (kupé pro 5), Easy
Night“). Ostatní kategorie jsou mezi nabídkami.

**Pozor:**
- Vnitrostátně v Česku (Praha – Děčín), Nizozemsku, Belgii, Francii,
  Švýcarsku a Itálii se neprodává, po Německu ano.
- Nejvýš 5 cestujících na rezervaci, větší skupině se cena neukáže.
- „Celé kupé“ má cenu za kupé (pro 4–5 lidí bývá nejlevnější). Kupé jen
  pro ženy se jako cena spoje nebere.
- Vlak se označuje „ES 452“. Jiní dopravci ho mohou psát jinak, pak se
  duplicitní spoje nesloučí.
- Na cesty mimo jeho stanice (Amsterdam → Bratislava, Brusel → Vídeň) ho
  zapojí hack `night_train`: noční vlak mezi stanicemi nejblíž začátku a cíli
  a k tomu dojezd a návaznost od ostatních dopravců (souřadnice stanic jsou
  v `COORDS`).
- Ceny se rychle mění s obsazeností: Amsterdam → Praha měsíc předem sedadlo
  50 €, lehátko 70 €; týden předem jen sedadlo za 170 €.

## MHD – zahraniční města (Transitous)

**Soubory:** `providers/city_transit.py`, `city.py` · **Typ:** JSON API Transitous · **Klíč:** ne

Jen pro cestu po jednom zahraničním městě (česká města hledá IDOS se
zastávkami z našeptávače a cenou podle tarifu IDS). Zastávky geokodérem
Transitous v okruhu 25 km od středu města, spojení celé i s pěšími přesuny.
Cena podle ceníku MHD (`city.FARES`, říjen 2026): Vídeň 3,20 € (Wiener Linien
od 1. 1. 2026), Bratislava 1,20 € / 1,80 € za 30 / 60 min (IDS BK od
1. 6. 2025), Budapešť 500 Ft na jednu linku, 600 / 850 Ft za 30 / 90 min
(BKK od 1. 6. 2025). Děti a senioři podle `city.DISCOUNTS` (zjednodušeně).

## CFR Călători – Rumunské dráhy

**Soubor:** `providers/cfr.py` · **Typ:** HTML formuláře `bilete.cfrcalatori.ro` (antiforgery token) · **Klíč:** ne

Umí jen **vnitrostátní spoje po Rumunsku**. Mezinárodní e-shop
(`bileteinternationale.cfrcalatori.ro`) chrání vyhledávání Cloudflare
Turnstile a ten neobcházíme.

**Postup:**
1. `GET /ro-RO/Itineraries` – ~1 700 stanic (`availableStations`
   v JavaScriptu stránky, cache 14 dní).
2. `GET /ro-RO/Rute-trenuri/{Odkud}/{Kam}?DepartureDate=dd.mm.yyyy&MinutesInDay=…`
   – skrytý formulář s `ConfirmationKey` a `__RequestVerificationToken`.
3. `POST /ro-RO/Itineraries/GetItineraries` – HTML se spoji (vlaky,
   přestupy, zastávky) a formulářem ceny u každého.
4. `POST /api/ro-RO/Itineraries/Price` – cena 2. třídy v lei.

**Cena:** endpoint vrací cenu za **jednoho** cestujícího. Program se ptá
zvlášť za dospělého (od 10 let) a dítě (5–9 let) a násobí počtem. Do 5 let
zdarma bez místa. Rumunské slevy (student, senior, TrenPlus) neuplatňujeme.

**Pozor:**
- Časy jsou rumunské (o hodinu víc než v Praze). Program je převádí.
- Hledá se jen jeden den od zadané hodiny. Ceny jen u prvních 16 spojů.
- Lehátka a lůžka se nenaceňují, jen sedadlo 2. třídy.
- Odpověď `ReCaptchaFailed` znamená blokaci (captchu neřešíme).
- Kurz RON bere program z ČNB. Záložní kurz je 4,95 Kč.

## SŽ – Slovinské dráhy

**Soubor:** `providers/sz.py` · **Typ:** HTML formuláře e-shopu `eshop.sz.si` · **Klíč:** žádný (stačí cookie relace)

Web potniski.sz.si je za Cloudflare a výsledky chrání výzva Turnstile, tu
neobcházíme. Prodejní e-shop **eshop.sz.si** ale odpovídá obyčejným
požadavkům.

**Postup:**
1. `POST /Home/TravelAutocomplete` – našeptávač stanic (interní ID
   e-shopu, cache 30 dní).
2. `POST /Home/FilterSingle` – formulář jednosměrné jízdenky na celý den.
   Posílá se **dvakrát**: `IcMcVc=false` (regionální vlaky a náhradní
   autobusy) a `IcMcVc=true` (jen spoje s vlakem IC/ICS/EC/MV). Výsledky
   se sloučí.
3. Z HTML se čte cena spoje a rozpis úseků (nástup, vlak, výstup).

**Cena:** za celou skupinu v EUR, 2. třída:
- dospělí od 15 let plné jízdné;
- děti 6–14 let poloviční;
- do 6 let zdarma.

U IC/ICS/EC je v ceně příplatek. Slevy (senioři, mládež) neuplatňujeme.

**Pozor:**
- E-shop prodává **jen vnitrostátní jízdenky**. Na cesty do zahraničí se
  SŽ neptá.
- `IcMcVc=true` neznamená „i IC vlaky“, ale „jen spoje s IC vlakem“. Bez
  druhého dotazu chybí buď rychlé, nebo regionální spoje.
- Odpověď chodí i jako Brotli, proto posíláme
  `Accept-Encoding: gzip, deflate`.
- „Vlak“ `BUS 60606` je náhradní autobus. Upozornění e-shopu se přidá ke
  spoji jako poznámka.

## HŽPP – Chorvatské dráhy

**Soubor:** `providers/hzpp.py` · **Typ:** JSON API e-shopu `prodaja.hzpp.hr/hzpp-sales-middleware-web` · **Klíč:** žádný (hlavičky `X-device-type`, `deviceType: WEB`, `Language: hr`)

**Postup:**
1. `GET stations` – ~500 chorvatských stanic (cache 7 dní). Hledá se
   lokálně, město znamená „Glavni kol.“, česká jména (Záhřeb) se převádějí.
2. `GET journeys-with-details/busInfo?…&transportChanges=DIRECT|ALL` –
   spoje celého dne, přímé i s přestupy. Spoje, které jiný spoj předjede,
   se zahodí.

**Cena:** `Tickets[].FinalPrice` v eurocentech za **všech N cestujících**
(běžné jízdné), 2. třída. Obsahuje slevu za online nákup i případnou akci.
Děti do 6 let se nepočítají (jedou zdarma).

**Pozor:**
- E-shop prodává **jen vnitrostátní jízdenky**. Na cesty do zahraničí se
  HŽPP neptá.
- Dětské jízdné online není. Děti 6–11 let program počítá jako dospělé, takže
  cena je horní odhad a spoj nese poznámku.
- `transportChanges=ALL` na dlouhých tratích (Záhřeb → Split) končí po
  ~2 min chybou 504. Program se ptá jednou s limitem 40 s. Když to selže,
  zůstanou přímé vlaky a poznámka.
- API nevrací mezizastávky.

---

## Vy – Norské dráhy

**Soubor:** `providers/vy.py` · **Typ:** JSON API webu vy.no · **Klíč:** žádný

**Postup:**
1. `GET /api/location/places/v2/autosuggest?query=…` – místa z registru
   Entur (`NSR:StopPlace:…`). Celý objekt místa se posílá dál beze změny.
2. `POST /api/global-itinerary/travel-planner/search` – spoje (plánovač
   Entur) s mezizastávkami a dopravcem (Vy, SJ Nord, Go-Ahead, autobusy
   Vy). Další stránka: `FIND_JOURNEY_SCROLL_LATER`.
3. `POST /api/offer/offer` s `itineraryIds` – `minimumPrice` **za celou
   skupinu** (v øre).

**Cestující:** cenu určuje věk – do 5 let zdarma, 6–17 let dětská cena,
od 18 dospělý. Senior 67+ se posílá s kategorií `senior-citizen` (−50 %).

**Pozor:**
- API chrání **AWS WAF**: občas vrátí `202` s hlavičkou
  `x-amzn-waf-action: challenge`. Výzvu neřešíme, jen to zkusíme znovu;
  když trvá, hlásí se blokace.
- Spoje do Švédska (Resrobot) vy.no neprodává – `BOOKABLE_ON_EXTERNAL_SITE`
  s cenou 0, tu nebereme.
- Itinerář platí krátce – ceny se žádají hned po hledání.

## DSB – Dánské dráhy

**Soubor:** `providers/dsb.py` · **Typ:** JSON API e-shopu dsb.dk · **Klíč:** krátkodobý `x-api-proof`

**Postup:**
1. `GET /api/domestic/v1/proof/` – token na ~2 minuty (hlavička
   `x-api-proof`). Bez něj API vrací 401.
2. `GET /api/domestic/v1/location/?input={"searchText": …}` – stanice.
3. `GET …/search/?input=…` – spoje **i s cenami** pro celou skupinu
   (Standardbillet, Orange, Orange Fri); `next` stránkuje (max ~6 spojů na
   stránku).

**Cestující:** `TT_CHILD_0_11`, `TT_CHILD_12_15`, `TT_YOUNG` (16–25),
`TT_ADULT`, `TT_SENIOR` (67+). Pravidlo „dospělý veze zdarma dvě děti do
11 let“ uplatňuje API samo.

**Pozor:**
- Bez hlavičky `Origin: https://www.dsb.dk` vrací 403.
- E-shop prodává jen vnitrostátní jízdenky. Øresundståg do Malmö ukáže bez
  ceny, Německo nezná.
- Našeptávač je velkorysý („Kodaň“ najde ulici Kodansvej) – bereme jen
  zastávky začínající hledaným slovem, přednostně vlakové.

## SJ – Švédské dráhy

**Soubor:** `providers/sj.py` · **Typ:** JSON API `prod-api.adp.sj.se` · **Klíč:** veřejný klíč z JS konfigurace webu

**Postup:**
1. Klíč `Ocp-Apim-Subscription-Key` se jednou denně přečte z
   `/assets/sjse-container/env-*.js` (záloha z října 2026).
2. `GET config` – všech ~6 500 stanic (kód UIC; země podle prefixu 74/76/86/80).
3. `POST search` → `departureSearchId`, `passengerListId`;
   `GET departures/search/{id}` – spoje celého dne bez cen.
4. `GET departures/{id}/offers?passengerListId=…` – cena jednoho spoje
   **za celou skupinu**; bereme nejlevnější 2. třídu (u nočních vlaků
   i lůžko).
5. Mezizastávky z provozního API (`trafficinfo-api/v2/rest/segments`).

**Cestující:** `CHILD_AND_YOUTH` 0–25 (s věkem), `ADULT` 26–64, `SENIOR`
65+. Děti do 6 let s doprovodem jedou zdarma na klíně a neposílají se.

**Pozor:**
- Ceny se zjišťují spoj po spoji (jako na webu) – jedno hledání je
  ~40 požadavků. Jízdní řád se ukáže hned, ceny dobíhají.
- SJ prodává i průběžné jízdenky s jinými dopravci (Mälartåg, Öresundståg,
  Vy Bus4You) a do Osla, Kodaně a Narviku.
- `NO_DEPARTURE` = spoj se neprodává, `FULLY_BOOKED` = vyprodáno.

---

## Cena spoj po spoji (ZSSK, SJ, CFR, PKP, IDS, BDŽ)

Dopravci, u kterých cena znamená zvláštní dotaz nebo průchod nákupem pro
**každý spoj**, oceňují jen první spoje: nejvýš **3 spoje** a jen do
**4 h 15 min** od zadaného odjezdu – co nastane dřív (`price_scope`
v `providers/base.py`). Spoje za tím se neukazují, takže výsledky těchto
dopravců mají vždy cenu. U ZSSK lze počet změnit volbou `price_limit`.

## BDŽ – Bulharské dráhy

**Soubor:** `providers/bdz.py` · **Typ:** HTML jízdního řádu razpisanie.bdz.bg · **Klíč:** CSRF token ze stránky

E-shop tickets.bdz.bg ukáže cenu jen přihlášenému
(`api/Ticket/GetTicket?onlyPrice=true` → 401). Veřejný jízdní řád ale má
u každého spoje ceník.

**Postup:**
1. `GET /bg/{odkud}/{kam}/{DD.MM.RRRR}` – spoje dne; stanice jsou slugy
   („sofia“) ze seznamu `window.stationsData`. Karta spoje má
   `data-price-index` = klíč do `BDTJSONItemsPriceAPI.trains`.
2. `POST /bg/route/…` a `POST /bg/price/…` s vlaky spoje (hlavička
   `X-CSRF-Token`) – trasa se zastávkami a ceník (plné, dítě 7–10, skupina
   3–6 osob, místenka).

**Cena:** skládáme ze ceníku – děti do 7 let zdarma, 7–10 let dětské,
3–6 dospělých skupinová cena, u vlaku s povinnou místenkou + místenka.
BDŽ cenu označuje za orientační, spoj nese poznámku. Bulharsko platí eurem.

## CP – Portugalské dráhy

**Soubor:** `providers/cp.py` · **Typ:** JSON API api-gateway.cp.pt · **Klíč:** veřejné klíče z `/fe-config.json`

**Postup:**
1. `GET www.cp.pt/fe-config.json` – adresa `travelApiUrl` a klíče webu
   (`X-Api-Key`, `x-cp-connect-id`, `x-cp-connect-secret`).
2. `GET travel-api/stations` – stanice („94-30007“ = Lisboa Santa Apolónia).
3. `POST travel-api/journeys` – spoje celého dne se zastávkami a cenou
   (`basePrices`, `priceType` 1 = za osobu).

**Cestující:** děti 4–12 let −50 % (dopočítané), do 4 let zdarma; senioři
jako dospělí (sleva jen s dokladem).

**Pozor:** konfigurace obsahuje slovo „recaptcha“ – čte se jako text, jinak
by ji detekce blokace odmítla. Časy jsou lisabonské (o hodinu méně).

## Srbijavoz – Srbské dráhy

**Soubor:** `providers/srbijavoz.py` · **Typ:** JSON API eKarta (webapi1.srbvoz.rs) · **Klíč:** ne

**Postup:**
1. `GET eKarta/api/stanica/StaniceGetAll_MPS` – stanice (`sifra`).
2. `GET eKarta/api/listavozova/ListaVozova_Web?stanicaod&stanicado&datum=RRRR-M-D&brojputnika=n&razred=2`
   – vlaky dne se zastávkami a cenou **za n cestujících** (`cenau` =
   jízdné `cenao` + příplatek `cenad`), v dinárech.

**Pozor:** eKarta prodává jen některé vlaky – rychlovlaky Soko Bělehrad –
Novi Sad v ní nejsou (prázdný seznam). Jen přímé vlaky. Děti 4–15 let
−50 % z jízdného dopočítáváme. Dinár ČNB nevede – používá se nouzový kurz.

## SBB – Švýcarské dráhy

**Soubor:** `providers/sbb.py` · **Typ:** JSON API transport.opendata.ch + HTML starého jízdního řádu sbb.ch · **Klíč:** ne

Nový web sbb.ch (GraphQL `graphql.www.sbb.ch`) je za AWS CloudFront
a z datacentra vrací 403 všemu. **Starý jízdní řád** sbb.ch (JSF/PrimeFaces)
ale odpovídá a ukazuje u spojů cenu „od“.

**Postup:**
1. `GET transport.opendata.ch/v1/connections` – spoje se zastávkami
   a nástupišti (komunitní API nad daty SBB).
2. `GET www.sbb.ch/en/buying/pages/fahrplan/fahrplan.xhtml?von&nach&datum&zeit&an=false&suche=true`
   – stránka se spoji a formulářem `shopForm`. Jména stanic bereme švýcarská
   z kroku 1 („Curych“ by sbb.ch vyložil jako Chur).
3. `POST` na `action` formuláře jako PrimeFaces Ajax
   (`javax.faces.source=shopForm_ladeAbPreiseRC`, `partial.render` = id prvků
   s třídou `updatableForAbPreis`, hlavičky `Faces-Request: partial/ajax`,
   `X-Requested-With`, `Accept: application/xml…`, `Sec-Fetch-Mode: cors` –
   bez nich přijde stránka bez cen). Odpověď: „Tickets from CHF 26.50“,
   u úsporných „Supersaver ticket available“.
4. Ceny se ke spojům přiřadí podle času odjezdu a příjezdu.

**Cena:** sbb.ch anonymně ukazuje cenu **s polovičním předplatným Halbtax**
(Curych → Bern od CHF 26.50, plné jízdné kolem CHF 53–56). Halbtax je
přesně 50 %, proto dospělý = 2 × cena „od“, dítě 6–15 let = cena „od“, do
6 let zdarma. Platí to i pro úsporné jízdenky. Spoj nese poznámku s původní
cenou s Halbtaxem.

**Pozor:**
- Ceny jsou jen pro 5–6 spojů od zadaného času; spoje bez ceny se
  neukazují. Když se ceny nenačtou vůbec, zůstane jízdní řád s poznámkou.
- Dál do nákupu nejdeme – tlačítko nákupu chrání reCAPTCHA.
- Stránka je JSF: když se změní id prvků `shopForm_…`, ceny se nenačtou.

---

## SNCF (přes Kombo)

**Soubor:** `providers/sncf.py` · **Typ:** JSON API prodejce kombo.co · **Klíč:** žádný

SNCF Connect ani Trainline se z programu ptát nedají (DataDome, viz
[Nezapojení dopravci](#nezapojení-dopravci)). **Kombo** je oficiální
prodejce jízdenek SNCF (TGV INOUI, OUIGO, TER, Intercités, Lyria) a jeho
web volá API bez captchy.

**Postup:**
1. `GET place.kombo.co/search?term=…&locale=en` najde město (`i` = id,
   `c` = země).
2. `POST search.kombo.co/search` s městy, datem a cestujícími (věkové
   skupiny `Infant` 0–3, `Child` 4–11, `Young` 12–27, `Adult`, `Senior`
   60+) vrátí `key`.
3. `GET search.kombo.co/search/trips?key=…&lastIndex=…` se opakuje, dokud
   `percentageDone` není 100 (obvykle 2–5 s). Nádraží, města a dopravci
   jsou v `dependencies`.

**Cena:** v centech **za celou skupinu** (ověřeno: 2 dospělí = 2×,
dospělý + dítě 8 let na TGV INOUI = 1,5×). `subTrips[].bestPrice` je cena
dopravce (jako na sncf-connect.com) – tu bereme. `trip.bestPrice` je cena
u Komba s poplatkem ~18 %, ta je v poznámce.

**Pozor:**
- **Časy mají „Z“, ale jsou místní** (TGV 6611 z Paříže v 11:00 má
  „11:00:00.000Z“). Zóna se bere z města nádraží.
- Hledá se mezi městy (Paříž = všechna nádraží). Kombo vrací i autobusy –
  spoje bez vlaku zahazujeme. Necháváme i jiné vlaky po Francii
  (Frecciarossa Paříž – Lyon).
- Ptá se jen u cest, které začínají nebo končí ve Francii.
- Srovnávač Virail ukazuje stejná data, ale po pár hledáních vrací rate
  limit 429 – proto Kombo napřímo.

---

## Nezapojení dopravci

### SNCF Connect a Trainline

`sncf-connect.com` chrání ochrana **DataDome**:
- obyčejný HTTP dotaz dostane stránku „Please enable JS“;
- v prohlížeči projde úvodní stránka i našeptávač
  (`/bff/api/v1/autocomplete`);
- vyhledání spojů (`/bff/api/v1/itineraries`) končí HTTP 403 s captchou
  (`geo.captcha-delivery.com`).

**Trainline** (thetrainline.com) má stejnou ochranu: našeptávač
(`/api/locations-search/v2/search`) odpovídá, hledání spojů
(`POST /api/journey-search/`) vrací captchu DataDome s `t=bv` (zablokovaný
návštěvník), a to i ze skutečného prohlížeče. Omio a Rail Europe vrací 403
už na úvodní stránce.

Obcházet captchu nebudeme, proto se vlaky SNCF hledají přes
[Kombo](#sncf-přes-kombo).

---

### Italo (Itálie)

Web `biglietti.italotreno.com` volá `api-biglietti.italotreno.com/api/v1/`.
Stanice (`stations`) fungují bez přihlášení. Hledání spojů (`POST booking`)
ale vyžaduje anonymní token (`BIGSessionToken`) z
`POST biglietti.italotreno.com/api/login`, a ten chrání Akamai Bot Manager:
- přímý dotaz dostane 403 „Access Denied“;
- prohlížeč (Playwright) se na italotreno.com z datacentra vůbec nepřipojí.

Obcházet ochranu nebudeme. Z domácího připojení by přihlášení možná
prošlo, ale odpověď hledání se nedala ověřit, proto Italo zapojené není.
Vlaky Italo v Itálii zatím neuvidíte; Trenitalia prodává jen své vlaky.

### Mezinárodní e-shopy CFR a SŽ

Mezinárodní prodej CFR (`bileteinternationale.cfrcalatori.ro`) chrání
Cloudflare Turnstile. Web potniski.sz.si (jízdní řád SŽ) taky. Zapojené
jsou proto jen vnitrostátní e-shopy.

---

## Jak přidat dopravce

1. Nový soubor `vyhledavac/providers/<jmeno>.py` s třídou odvozenou od
   `HttpProvider` (API) nebo `BrowserProvider` (prohlížeč) a dekorátorem
   `@PROVIDERS.register()`.
2. Vlastnosti třídy:
   - `name`, `label`, `currency`, `countries`;
   - `multi_passenger` – umí cenu za skupinu?
   - `international_only` – po Česku se neptat;
   - `hack_legs` – smí se na něj ptát hacky na dílčí úseky?
   - `min_interval` – rozestup dotazů.
3. `async def search(self, query) -> list[Journey]`:
   - když dopravce na trasu nejezdí, vyhodit `StationNotFoundError` (bez
     zbytečných dotazů);
   - ceny tvořit přes `self.make_fare(částka, měna, label=…, tariff=…)`.
4. Cenu, která platí jen pro část trasy nebo je jen orientační, **nikdy
   neukládat jako cenu spoje**. Uložit do `journey.raw` a dát poznámku
   (viz ÖBB, Trenitalia).
5. Testy s nahranou odpovědí do `tests/` (data do `tests/data/`), zápis do
   tohoto dokumentu a do přehledu v README.
6. Nového dopravce zapnout ve výchozím výběru: `DEFAULT_PROVIDERS`
   v `cli.py`. Web zapíná automaticky všechny bez prohlížeče, pro uložený
   výběr přidat jméno do seznamu `added` v `web/static/app.js`.
