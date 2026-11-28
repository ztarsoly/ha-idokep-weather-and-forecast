# Időkép Weather & Forecast Home Assistant integráció

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/ztarsoly/ha-idokep-weather-and-forecast)](https://github.com/ztarsoly/ha-idokep-weather-and-forecast/releases)

Az [Időkép](https://www.idokep.hu) magyar időjárási szolgáltatás: weboldalán a települések aktuális időjárása és előrejelzése, időjárási riasztások, radarképek és időjárás-állomások mérései érhetők el.

Ez az egyéni (custom) Home Assistant integráció az idokep.hu adatait hozza be: aktuális időjárást, óránkénti és napi előrejelzést, időjárási riasztásokat, egy vagy több választható időjárás-állomás (automata) mért adatait és az országos radarképet.

Az integráció az idokep.hu nyilvános oldalait olvassa (nincs hivatalos API, felhasználói fiók sem kell). Ha az idokep.hu megváltoztatja az oldalait, az integrációt is frissíteni kell.

## Funkciók

- **Időjárás entitás** óránkénti és napi (kb. 23 napos) előrejelzéssel, a Home Assistant új forecast API-jával
- **Előrejelzési szenzorok**: hőmérséklet, állapot, napi maximum- és minimumhőmérséklet, szélirány, szélerősség, becsült szélsebesség és széllökés, csapadék, csapadék valószínűsége, valamint a település állomásainak átlagából páratartalom, harmatpont, légnyomás és UV-index, és ezekből számított hőérzet
- **Riasztások**: riasztás 3 órán belül, figyelmeztetés mára (nincs, sárga, narancs, piros; típusonként is) és vihar egy órán belül
- **Időjárás-állomások** (nem kötelező): egy vagy több idokep.hu automata 12 mért adata egy szolgáltatásban összevonva, belőlük számított értékek (harmatpont, hőérzet, nedves hőmérséklet, abszolút páratartalom, páranyomás, páranyomás-hiány, felhőalap) és a mérés ideje
- **Országos radar** (nem kötelező): animált GIF az utolsó 6 radarképből, külön szolgáltatásként
- Az előrejelzés, az állomások és a radar közül csak azt kell felvenni, amire szükséged van
- Magyar és angol fordítás, a Home Assistant nyelvének megfelelően
- Csak a szükséges oldalakat tölti le, és a hibás letöltést újrapróbálja; régi adatot nem mutat, kivéve az előrejelzést, amíg az nem tölthető le (újraindítás után is)

## Követelmények

- Home Assistant 2026.7 vagy újabb
- Internetkapcsolat a `www.idokep.hu` eléréséhez

Külön Python-csomag nem szükséges.

## Szolgáltatások és entitások

Egy bejegyzéshez a megadott településhez az **előrejelzés**, a megadott állomáskódokhoz az **időjárás** szolgáltatás jön létre; bármelyik elhagyható, de legalább az egyik kell. A `<Név>` a beállításkor megadott név. A szolgáltatások neve a Home Assistant nyelvét követi (angolul `<Név> Forecast`, `<Név> Weather`).

### `<Név> Előrejelzés` – Weather forecast (csak településsel)

Település nélküli bejegyzésben nincs sem ez a szolgáltatás, sem az entitásai (az időjárás entitás és a vihar szenzor sem), és az előrejelzés oldalai nem töltődnek le.

| Entitás | Típus | Megjegyzés |
|---|---|---|
| `<Név> Előrejelzés` | időjárás | aktuális állapot, hőmérséklet, szél, becsült széllökés (a becsült szélsebesség 1,75-szerese), páratartalom, harmatpont, hőérzet, légnyomás, UV-index, becsült felhőzet (%), az aktuális óra csapadéka (`precipitation` attribútum, mm); óránkénti és napi előrejelzés (felhőzettel) |
| Hőmérséklet | szenzor (°C) | aktuális |
| Állapot | szenzor (felsorolás) | pl. napos, derült éjszaka (napnyugta után), borult, eső |
| Felhőzet | szenzor (%) | **becslés** az aktuális állapot leírásából (0, 25, 50, 75 vagy 100%; lásd lent) |
| Napi maximumhőmérséklet | szenzor (°C) | a napi előrejelzésből, éjfélig megmarad |
| Napi minimumhőmérséklet | szenzor (°C) | a napi előrejelzésből, éjfélig megmarad |
| Szélirány (fok) | szenzor (°) | az aktuális óra előrejelzéséből |
| Szélirány (égtáj) | szenzor (felsorolás) | 16 égtáj (É, ÉÉK, ÉK, …) |
| Szélerősség | szenzor (felsorolás) | Beaufort-skála 0–12 (Szélcsend … Orkán) |
| Szélsebesség | szenzor (km/h) | **becslés**: a szélerősség Beaufort-fokozatának alsó határa (az idokep.hu nem ad számot) |
| Széllökés | szenzor (km/h) | **becslés**: a becsült szélsebesség 1,75-szerese |
| Csapadék | szenzor (mm) | az aktuális óra csapadéka az előrejelzésből (0,1 mm pontossággal) |
| Csapadék valószínűsége | szenzor (%) | az aktuális óra előrejelzéséből; 0, ha az oldal nem mutat értéket |
| Páratartalom | szenzor (%) | a település állomásainak átlaga a páratartalom-térképről (`/para`) |
| Harmatpont | szenzor (°C) | a település állomásainak átlaga a harmatpont-térképről (`/para`) |
| Hőérzet | szenzor (°C) | számított a hőmérsékletből, a páratartalomból és a **becsült** szélsebességből (Steadman-képlet, napsugárzás nélkül), minden frissítéskor |
| Légnyomás | szenzor (hPa) | a település állomásainak átlaga a légnyomástérképről (`/legnyomas`), tengerszintre átszámítva |
| UV-B index | szenzor | a település állomásainak átlaga az UV-térképről (`/uv`); kevés állomás méri |
| Riasztás 3 órán belül | szenzor (felsorolás) | a legmagasabb szint az óránkénti kártyák riasztás-ikonjaiból: Nincs riasztás, Sárga, Narancs vagy Piros riasztás; az idokep.hu ezeket az aktuális és a következő két órához teszi ki. Az `alerts` attribútum az érvényes riasztásokat sorolja fel |
| Figyelmeztetés mára | szenzor (felsorolás) | az oldal tetején lévő sáv(ok) legmagasabb szintje, amely riasztás vagy figyelmeztetés is lehet: Nincs figyelmeztetés, Sárga, Narancs vagy Piros figyelmeztetés. A `warnings` attribútum az érvényes figyelmeztetéseket sorolja fel |
| Vihar várható egy órán belül | bináris szenzor | bekapcsol, ha az aktuális időjárás viharos (10 percenként frissül), vagy az előrejelzésben az aktuális vagy a következő óra viharos (óránként frissül). Viharos: zivatar, száraz zivatar, villámlás, jégeső, eső viharos széllel, porvihar. Ha az aktuális időjárás oldala nem tölthető le, helyette az előrejelzés aktuális órája számít. Kikapcsolva csak akkor, ha mindkét forrás ismert; ha egyik sem jelez vihart, de valamelyik hiányzik, nem elérhető |

Az `alerts` és a `warnings` attribútum csak az érvényben lévő típusokat tartalmazza, típusonként a legmagasabb szinttel. Elöl a legmagasabb szint áll, az azonos szintűek a lenti típussorrendben követik egymást, például:

```yaml
alerts:
  - label: Szél
    type: wind
    level: yellow
```

- `label`: a típus neve a Home Assistant nyelvén
- `type`: az idokep.hu saját típusneve angolul: `thunderstorm` (zivatar), `wind` (szél), `rain` (eső), `freezing_rain` (ónos eső), `blowing_snow` (hófúvás), `snow` (havazás), `fog` (köd), `frost` (fagy), `heat` (hőség); `other`, ha a szövegből nem derül ki
- `level`: `yellow`, `orange` vagy `red`

Az integráció entitásai „nem elérhető” (unavailable) állapotúak, ha nincs értékük, „ismeretlen” állapot nincs: ha az adat letöltése nem sikerült, ha az oldal nem adja meg, vagy ha a településnek nincs ilyen értéket mérő állomása (páratartalom, harmatpont, hőérzet, légnyomás, UV-B index). Az időjárás entitás akkor nem elérhető, ha az aktuális időjárás állapota hiányzik vagy ismeretlen leírású; ilyenkor az előrejelzése sem érhető el rajta.

Ha az **aktuális időjárás oldala** nem tölthető le, az időjárás entitás állapota, hőmérséklete és felhőzete az előrejelzés aktuális órájából jön, így az entitás elérhető marad; a vihar szenzor is ezt az órát tekinti az aktuális időjárásnak. A Hőmérséklet, az Állapot, a Felhőzet és a Hőérzet szenzor ilyenkor nem elérhető.

**Kivétel az előrejelzés:** ha az előrejelzési oldal nem tölthető le, az előrejelzés-szenzorok és az időjárás entitás előrejelzése az utolsó sikeresen letöltött (újraindítás után a mentett) előrejelzést követik óráról órára: az aktuális órától és a mai naptól kezdve, a szelet, a csapadékot és a vihart az aktuális órából, a mai max/min értéket a mai napból. A mentettből hiányzó órák pótlódnak (lásd lentebb), ha van benne az aktuálisnál későbbi óra. Csak akkor nincs előrejelzés, ha a mentettben sem az aktuális, sem későbbi óra nincs. A riasztás és a figyelmeztetés ilyenkor is nem elérhető.

A Felhőzet szenzor és az időjárás entitás felhőzete (`cloud_coverage`) **becslés** az idokep.hu állapotleírásából, mert az oldal nem ad számszerű értéket. A felhőszavak a nyolcadokat követik: derült és napos 0%, gyengén felhős 25%, közepesen felhős 50%, erősen felhős 75%, borult 100%. A tartós eső, hó, ónos eső és köd 100%, a zápor, hózápor, zivatar, hózivatar, villámlás és jégeső 75%, a felhőzetről semmit nem mondó leírások (párás, pára, szeles, hófúvás, porvihar) 50%. Ismeretlen leírásnál nem elérhető.

### `<Név> Időjárás` – Current weather (csak állomáskóddal)

Egy vagy több idokep.hu időjárás-állomás (automata) mért adatai, egy szolgáltatásban. Több állomásnál minden érték a választott **összevonás módja** szerint áll össze:

- **Prioritás**: az első olyan állomás értéke a listában, amelynek van elég friss mérése; a következő állomás pótolja, amit az előző nem mér, vagy ha épp nem érhető el.
- **Legfrissebb**: annak az állomásnak az értéke, amelynek a legutolsó az elég friss mérése (egyenlőségnél a listában előbbié).
- **Átlag**: a friss mérések átlaga (a szélirány szögként átlagolva).

Minden szenzor `stations` attribútuma mutatja, melyik állomás(ok)tól származik az érték. Azok az értékek, amelyeket egyik állomás sem mér (vagy még sosem mért), vagy amelyekre egyiknek sincs a **Mérés maximális kora** beállításnál (alapból 60 perc) frissebb mérése, „nem elérhető” állapotúak (a számított értékek akkor, ha valamelyik bemenetükre ez igaz). Ha egy állomás épp nem érhető el, a többi adatai jelennek meg; a korábbi méréseit nem használja.

| Entitás | Mértékegység | Megjegyzés |
|---|---|---|
| Hőmérséklet | °C | mért |
| Relatív páratartalom | % | mért |
| Légnyomás | hPa | mért |
| Csapadék az elmúlt 24 órában | mm | mért |
| Csapadék éjfél óta | mm | mért |
| Csapadékintenzitás | mm/h | mért |
| Szélsebesség | km/h | mért |
| Széllökés | km/h | mért |
| Szélirány | ° | mért |
| UV-B index | – | mért |
| Globálsugárzás | W/m² | mért |
| Vízhőmérséklet | °C | mért |
| Harmatpont | °C | számított (Magnus-képlet) a hőmérsékletből és a relatív páratartalomból |
| Hőérzet | °C | számított (Steadman-féle látszólagos hőmérséklet, napsugárzás nélkül) a hőmérsékletből, a relatív páratartalomból és a szélsebességből |
| Nedves hőmérséklet | °C | számított (Stull-képlet) a hőmérsékletből és a relatív páratartalomból |
| Abszolút páratartalom | g/m³ | számított: egy köbméter levegő víztartalma |
| Páranyomás | hPa | számított: a levegő nyomásából a vízgőzre jutó rész (a levegő víztartalmának mértéke) |
| Páranyomás-hiány | kPa | számított: mennyire van távol a levegő a telítettségtől |
| Felhőalap | m | számított **becslés** az állomás fölött (125 m a hőmérséklet és a harmatpont minden °C különbségére); gomolyfelhőkre érvényes |
| Mérés ideje | időbélyeg | a legfrissebb felhasznált mérés ideje |

### `Országos Radarkép` (National Radar Image) – Radar szolgáltatás

Amíg nincs **Radar** bejegyzés, a beállítás űrlapján megjelenik az **Országos radarkép** jelölőnégyzet (alapból bejelölve); bejelölve létrejön egy **Radar** bejegyzés egy kép entitással (**Országos radarkép**, angolul National radar image). Ez az országos radar utolsó 6 képéből készült animált GIF (képenként 1 mp, az utolsón 2 mp). Ha törlöd, magától nem jön létre újra, a jelölőnégyzet viszont ismét megjelenik. Később úgy veheted fel, hogy új bejegyzést adsz hozzá, és a nevet, a települést és az állomáskódokat üresen hagyod. A kikapcsolt Radar bejegyzés is meglévőnek számít (nincs jelölőnégyzet); ha kell, kapcsold be. Ha a letöltés nem sikerül, a kép nem elérhető, amíg egy letöltés újra nem sikerül; ekkor mind a 6 képet újra letölti.

## Telepítés

### HACS-on keresztül

1. Nyisd meg a **HACS**-t, kattints a jobb felső **⋮** gombra, és válaszd az **Egyéni tárolók** (Custom repositories) menüpontot.
2. Add meg a tároló címét: `https://github.com/ztarsoly/ha-idokep-weather-and-forecast`, típusnak válaszd az **Integráció** (Integration) lehetőséget.
3. Keresd meg az **Időkép Weather & Forecast** integrációt, és telepítsd.
4. Indítsd újra a Home Assistantot.

### Kézzel

1. Töltsd le az `idokep_weather_and_forecast-<verzió>.zip` fájlt a [legutóbbi kiadásból](https://github.com/ztarsoly/ha-idokep-weather-and-forecast/releases/latest).
2. Csomagold ki a Home Assistant konfigurációs mappájában (ahol a `configuration.yaml` van) lévő `custom_components` mappába; ha nincs ilyen mappa, hozd létre. A végén a `custom_components/idokep_weather_and_forecast/manifest.json` fájlnak kell meglennie.
3. Indítsd újra a Home Assistantot.

Frissítéshez töröld a `custom_components/idokep_weather_and_forecast` mappát, csomagold ki ugyanígy az új kiadás zip-fájlját, és indítsd újra a Home Assistantot.

## Beállítás

**Beállítások → Eszközök és szolgáltatások → Integráció hozzáadása → Időkép Weather & Forecast**

| Mező | Kötelező | Leírás |
|---|---|---|
| Név | igen* | A bejegyzés és a szolgáltatások neve (pl. Otthon → „Otthon Előrejelzés”, „Otthon Időjárás”) |
| Település | nem* | Üresen hagyva nincs előrejelzés. A település neve úgy, ahogy az idokep.hu címében szerepel, pl. `Budapest` vagy `Debrecen-Józsa` (`https://www.idokep.hu/idojaras/Debrecen-Józsa`); az ismeretlen nevet az integráció nem fogadja el. A kis- és nagybetű nem számít, és a név az idokep.hu írásmódjával mentődik (pl. `pecs` → `Pécs`); ékezet nélkül beírva az oldal nem minden települést ismer fel |
| Időjárás-állomások kódjai | nem* | Egy vagy több idokep.hu automata kódja fontossági sorrendben, pl. `jozsamet` (`https://www.idokep.hu/automata/jozsamet`) |
| Összevonás módja | nem | Prioritás (alapértelmezett), Legfrissebb vagy Átlag – lásd fent |
| Mérés maximális kora | nem | Az ennél régebbi állomásmérés nem számít, percben (1–1440, alapból 60) |
| Országos radarkép | nem | Jelölőnégyzet, csak akkor látszik, ha még nincs **Radar** bejegyzés; alapból bejelölve, és ekkor felveszi |

\* A névhez a település vagy legalább egy állomáskód is kell. Ha csak a radart szeretnéd, hagyd üresen a nevet, a települést és az állomáskódokat, és a jelölőnégyzet maradjon bejelölve.

Az állomáskódot az [idokep.hu/automata](https://www.idokep.hu/automata) oldalon találod: válaszd ki az állomást a listából, a kód a címben a `/automata/` utáni rész (a listában zárójelben is látszik).

Egy település csak egyszer vehető fel. Több települést külön bejegyzésként adhatsz hozzá. Település nélkül ugyanazokkal az állomáskódokkal (a sorrendtől függetlenül) csak egy bejegyzés vehető fel.

### Módosítás később

A bejegyzés **Beállítás** (Configure) gombjával módosítható:

- **Név**: a bejegyzés és a szolgáltatások neve azonnal megváltozik. Ha egy általad átnevezett szolgáltatás nevében szerepel a bejegyzés régi neve, abban is lecserélődik; az entitások azonosítói megmaradnak.
- **Település**: más településre vált; a bejegyzés és a szolgáltatások neve, valamint az entitások azonosítói megmaradnak. Törölve a `<Név> Előrejelzés` szolgáltatás, az entitásai és a mentett előrejelzés megszűnnek; ha újra megadod, ismét létrejönnek.
- **Időjárás-állomások kódjai**: állomások hozzáadása, törlése vagy sorrendjük módosítása; mind törölve a `<Név> Időjárás` szolgáltatás megszűnik.
- **Összevonás módja**: Prioritás, Legfrissebb vagy Átlag.
- **Mérés maximális kora**: az ennél régebbi állomásmérés nem számít, percben.

A település és az állomáskódok közül legalább az egyiknek meg kell maradnia.

A bejegyzés a Home Assistant saját **Átnevezés** menüpontjával is átnevezhető: ilyenkor a szolgáltatások neve csak a bejegyzés újratöltése vagy a Home Assistant újraindítása után követi az új nevet, az általad átnevezett szolgáltatások neve pedig nem változik.

## Eltávolítás

1. **Beállítások → Eszközök és szolgáltatások → Időkép Weather & Forecast**: töröld a bejegyzéseket (a bejegyzés **⋮** menüjében **Törlés**). A hozzájuk tartozó szolgáltatások, entitások és a mentett előrejelzés is törlődnek.
2. Ha van **Radar** bejegyzés, azt is töröld.
3. Ha HACS-on keresztül telepítetted: a HACS-ban nyisd meg az **Időkép Weather & Forecast** integrációt, és válaszd a **⋮ → Eltávolítás** (Remove) menüpontot. Kézi telepítésnél töröld a `custom_components/idokep_weather_and_forecast` mappát.
4. Indítsd újra a Home Assistantot.

Az idokep.hu oldalán nincs teendő: az integráció nem hoz létre fiókot, és semmit nem küld fel.

## Frissítési ütemezés

| Adatforrás | Mit frissít | Ütemezés | Hiba esetén |
|---|---|---|---|
| Aktuális időjárás (`/idojaras/`) | hőmérséklet, állapot | 10 percenként (x:00:59, x:10:59, x:20:59, …) | percenként újra |
| Előrejelzés (`/elorejelzes/`) | óránkénti és napi előrejelzés, mai max/min, szél, riasztások | óránként (x:00:59) | 10 percenként újra |
| Időjárás-állomások (`/automata/<kód>`) | állomás szenzorai | 5 percenként (x:00:59, x:05:59, …), minden állomás egyszerre | percenként újra, amíg valamelyik nem érhető el |
| Páratartalom-térkép (`/para`) | páratartalom, harmatpont | 10 percenként (x:00:59, x:10:59, x:20:59, …) | percenként újra |
| Légnyomástérkép (`/legnyomas`) | légnyomás | 10 percenként (x:00:59, x:10:59, x:20:59, …) | percenként újra |
| UV-térkép (`/uv`) | UV-B index | 10 percenként (x:00:59, x:10:59, x:20:59, …) | percenként újra |
| Radar (`/radar/`) | radarkép | 5 percenként (x:00:59, x:05:59, …) | percenként, amíg új kép nem érkezik (hiba után is) |

- Indításkor (újraindítás, újratöltés, új bejegyzés) minden adat azonnal frissül. Ami ekkor nem sikerül, az percenként újrapróbálódik az első sikeres letöltésig (az előrejelzés is); utána a táblázat szerinti ütemezés érvényes.
- Ha induláskor semmit sem lehetne mutatni, a bejegyzés nem indul el: a Home Assistant az Eszközök és szolgáltatások oldalon a hiba okával együtt jelzi, és egyre ritkábban (legfeljebb 10 percenként) újrapróbálja. Településsel rendelkező bejegyzésnél ez az az eset, amikor a település egyik oldala sem érhető el, és az aktuális vagy későbbi órát tartalmazó mentett előrejelzés sincs; település nélküli bejegyzésnél az, amikor egyik állomás adatfájlja sem tölthető le; a Radar bejegyzésnél az, amikor a radar nem érhető el. Az integráció ilyenkor legfeljebb hibakeresési (debug) szinten ír a naplóba, és azt sem jelzi, ha utána sikerül.
- Ha egy bejegyzés településnevét az idokep.hu nem ismeri (egy korábbi verzió még elmenthetett ilyet), a bejegyzés hibaüzenettel nem indul el, és nem is próbálkozik újra, mert az oldal ilyenkor Budapest időjárását adná. Javítsd a nevet a bejegyzés **Beállítás** (Configure) gombjával. Ez induláskor derül ki; ha az aktuális időjárás oldala épp nem érhető el, az előrejelzési oldal címéből, ha pedig az sem, a következő induláskor.
- A sikertelen letöltés entitásai „nem elérhető” állapotúak, a korábbi adatot nem mutatják; az előrejelzés a fenti kivétel.
- Az ütemezett letöltés akkor is megtörténik, ha korábban jön, mint az újrapróbálás (az előrejelzésé teljes órakor).
- A naplóba forrásonként (oldal, állomás, radar) egy figyelmeztetés kerül, amikor a letöltés először nem sikerül, és egy, amikor újra sikerül; a közbenső hibák nem. A bejegyzés újratöltése (és a Home Assistant újraindítása) után elölről kezdi: a még mindig sikertelen letöltésre újra figyelmeztet, de ha az újratöltés után rögtön sikerül, nem jelzi, hogy újra sikerül.
- Váratlan hibánál a figyelmeztetés a hiba részleteit (traceback) is tartalmazza.
- Az ismeretlen időjárás-leírásról és szélerősségről bejegyzésenként és indulásonként, értékenként egy figyelmeztetés kerül a naplóba.
- Csak az az oldal töltődik le, amelyet legalább egy bekapcsolt entitás használ; a többi újraindításkor sem. A térképoldalakhoz a saját szenzoraik tartoznak: ha egy oldal minden szenzorát kikapcsolod, az oldal letöltése leáll, és az időjárás entitás megfelelő attribútumai is eltűnnek. Az előrejelzési oldalt az időjárás entitás is használja, így az csak akkor áll le, ha az is ki van kapcsolva.
- Ha a bejegyzés rendszerbeállításaiban kikapcsolod a lekérdezést (**Enable polling for updates**), az ütemezett frissítés leáll.

## Adattárolás

Az előrejelzési oldal legutóbbi sikeresen letöltött óránkénti és napi előrejelzése a Home Assistant `config/.storage/idokep_weather_and_forecast.<bejegyzés azonosító>` fájljába kerül. A fájl a bejegyzés törlésekor, illetve a település törlésekor törlődik.

A mentett előrejelzés két célt szolgál, újraindítás után is:

- amíg az előrejelzés nem tölthető le, ez látszik, óráról órára haladva (lásd fent);
- pótolja a sikeres letöltésekből hiányzó részeket: az aktuális órától hiányzó órákat (az aktuális órából jön az aktuális szél és csapadék), és a mai napot (napi előrejelzés, mai max/min), amelyet az oldal este már nem mutat.

Minden más adat (aktuális időjárás, riasztások, térképek, állomásmérések, radar) csak a friss letöltésből származik: amíg az nem sikerül, az érintett entitások nem elérhetők.

## Megjegyzések

- Az integráció minden kerekítése a felet a nullától távolabb kerekíti (pl. 2,5 → 3 és −2,5 → −3).
- A szélsebesség a Beaufort-fokozat alsó, a széllökés ennek 1,75-szerese, tehát mindkettő **becslés**; a valódi széllökés ennél nagyobb is lehet. Mért értékeket az időjárás-állomás ad.
- Este, amikor az idokep.hu már nem mutatja a mai napot, a mai napi előrejelzés és a mai max/min éjfélig megmarad. Ha nincs korábbról ismert mai adat, a mai nap az óránkénti előrejelzés mai óráiból áll össze (legmagasabb és legalacsonyabb hőmérséklet, csapadékösszeg, leggyakoribb időjárás, átlagos felhőzet); ez csak az ott szereplő órákat tükrözi, tehát este a nap hátralévő részét.
- Az idokep.hu az óránkénti előrejelzést a következő órától listázza. Az aktuális órától kezdve minden hiányzó óra pótlódik:
  - egy korábbi lekérdezésből (mentett előrejelzésből), ha az tartalmazza;
  - különben, ha nincs előtte meglévő (listázott vagy mentett) óra, az első utána következőnek a másolata (például közvetlenül a bejegyzés felvétele vagy a település módosítása után);
  - két meglévő óra között (az előtte lévő lehet már elmúlt is) a kettőből interpolálva, a lista szokásos pontosságával (a csapadék két tizedesre, a többi egész számra): a hőmérséklet, a felhőzet, a csapadék, a csapadék valószínűsége, a szélirány (a rövidebb úton) és a szélerősség fokozata (ebből a szélsebesség és a széllökés) arányosan, az időjárás és a vihar jelzése a közelebbi órából (félúton a későbbiből).

  Az óránkénti előrejelzés így az aktuális órával kezdődik, és ebből az órából jön az aktuális szél, csapadék és csapadék valószínűsége (így a hőérzet is).
- Az állomásadatok az idokep.hu grafikonjai mögötti adatfájlból származnak, ami nem hivatalos felület, és megváltozhat.

## Köszönetnyilvánítás

Ez az integráció [FabianGabor](https://github.com/FabianGabor) [HA-Idokep](https://github.com/FabianGabor/HA-Idokep) integrációjára épül. Köszönet az eredeti munkáért!

Az időjárási adatok forrása az [Időkép](https://www.idokep.hu). Ez a projekt nem az Időkép hivatalos terméke.

## Licenc

[MIT](LICENSE)
