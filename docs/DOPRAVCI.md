# Dopravci – jak program získává spojení a ceny

U každého dopravce tu najdete:
- odkud bere data (adresy API, postup);
- jak hledá stanice;
- jak počítá cestující a slevy;
- jakou cenu vrací (měna, za koho, co obsahuje);
- kdy se ho program vůbec ptá;
- na co si dát pozor.

Na konci je přehled dopravců, které zapojit nešlo (SNCF), a jak dopravce
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
  [Trenitalia](#trenitalia)
- [Nezapojení dopravci (SNCF)](#nezapojení-dopravci)
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
| Arriva | `arriva.py` | JSON API | vždy | skupina, CZK | ověřeno živě |
| Bileto | `bileto.py` | JSON API | vždy | skupina, CZK | ověřeno živě |
| Leo Express | `leo.py` | GraphQL API | jen jeho stanice | skupina, CZK | ověřeno živě |
| IDS | `ids_idos.py`, `idos.py` | IDOS + tarify IDS | jeden IDS / zastávka bez nádraží | skupina, z tarifu | ověřeno živě |
| ÖBB | `obb.py`, `_hafas.py`, `_oebb_shop.py` | HAFAS + e-shop API | ne po Česku | skupina, EUR | jízdní řád živě; e-shop z datacentra blokován |
| ZSSK | `zssk.py` | prohlížeč | ne po Česku | 1 osoba, EUR | ověřeno živě |
| PKP | `pkp.py` | Koleo API | konec v Polsku | 1 dospělý, PLN | ověřeno živě |
| MÁV | `mav.py` | JSON API e-shopu | konec v Maďarsku | skupina, EUR/HUF | ověřeno živě |
| DB | `db.py` | API int.bahn.de (+ prohlížeč) | konec v DE/NL/BE/FR/CH/DK/LU | skupina, EUR | stanice živě; spoje z datacentra blokovány |
| NS | `ns.py` | API ns.nl | konec v Nizozemsku | skupina, EUR (jen NL) | ověřeno živě |
| Trenitalia | `trenitalia.py` | API lefrecce.it | konec v Itálii | skupina, EUR (jen IT) | ověřeno živě |
| SNCF | – | – | – | – | nezapojeno (DataDome) |

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
všechny spoje včetně autobusů a MHD. Cenu pak spočítá tarif systému
(PID, IDESKA, IDS JMK, IDOL, IREDO, DÚK, IDPK, VDV, IDZK, IDSOK, ODIS).
IDS KK zapojený není.

**Postup:**
1. Výsledky `idos.cz/vlakyautobusymhdvse/spojeni/vysledky/?f=…&t=…`
   (nejvýš 2 stránky po 3 spojích). Úseky se dělí podle
   `div.outside-of-popup…`, i pěší přesuny.
2. Pro autobusy a vlaky ČD načte **detail spoje** (`/spojeni/draha/`,
   cache 24 h). Získá z něj:
   - zastávky, kde vozidlo opravdu staví, s časy;
   - systém linky („linka 300 [IDESKA]“), jinak seznam linek z JDF
     (`data/ids_lines.json.gz`, obnova
     `python tools/build_ids_lines.py --jdf JDF.zip`).
3. Zóny zastávek z databáze `data/stations.json.gz`. Stejnojmenné obce
   (Domanín u Třeboně × Domanín na Moravě) rozliší podle systému sousedních
   zastávek.
4. **Plánovač jízdenek** (dynamické programování) najde nejlevnější pokrytí
   jízdenkami IDS. Vlak smí rozdělit jen ve stanici, kde staví, i mezi
   dvěma systémy v přesahu (IDESKA do Benešova + PID do Prahy). Úseky mimo
   IDS (dálkový autobus, EC) zůstanou k dokoupení. Hack `idos_complete`
   se na ně zeptá dopravců (Arriva/SJT, Bileto, RegioJet, FlixBus, Leo
   Express a, jsou-li zapnuté, ČD a OneTicket) a vybere nejlevnější rozpis
   na **tentýž spoj**.

**Kdy se ptá:** když jsou obě místa ve stejném IDS, nebo když je jeden
konec zastávka či obec bez nádraží. Takové místo ostatní dopravci
neznají.

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

---

## Nezapojení dopravci

### SNCF (Francie)

`sncf-connect.com` chrání ochrana **DataDome**:
- obyčejný HTTP dotaz dostane stránku „Please enable JS“;
- v prohlížeči projde úvodní stránka i našeptávač
  (`/bff/api/v1/autocomplete`);
- vyhledání spojů (`/bff/api/v1/itineraries`) končí HTTP 403 s captchou
  (`geo.captcha-delivery.com`).

Obejít captchu nejde a nebylo by to fér, proto SNCF zapojené není.
Francouzské vlaky uvidíte v jízdním řádu ÖBB (HAFAS). Ceny tras přes
Německo do Francie (Paříž – Frankfurt ICE/TGV) zná DB.

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
