# Időkép Weather & Forecast Home Assistant integráció

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/ztarsoly/ha-idokep-weather-and-forecast)](https://github.com/ztarsoly/ha-idokep-weather-and-forecast/releases)

Egyéni (custom) Home Assistant integráció, amely az [idokep.hu](https://www.idokep.hu) adatait hozza be: aktuális időjárást, óránkénti és napi előrejelzést, időjárási riasztásokat, egy vagy több választható időjárás-állomás (automata) mért adatait és az országos radarképet.

Az integráció az idokep.hu nyilvános oldalait olvassa (nincs hivatalos API, felhasználói fiók sem kell). Ha az idokep.hu megváltoztatja az oldalait, az integrációt is frissíteni kell.

## Funkciók

- **Időjárás entitás** óránkénti és napi (kb. 23 napos) előrejelzéssel, a Home Assistant új forecast API-jával
- **Előrejelzési szenzorok**: hőmérséklet, állapot, mai maximum/minimum, szélirány, szélerősség, becsült szélsebesség
- **Riasztások**: vihar a következő órában, általános, sárga, narancs és piros riasztás
- **Időjárás-állomások** (nem kötelező): egy vagy több idokep.hu automata 12 mért adata egy eszközben összevonva, és a mérés ideje
- **Országos radar**: animált GIF az utolsó 5 radarképből, külön szolgáltatásként
- Magyar és angol fordítás, a Home Assistant nyelvének megfelelően
- Csak a szükséges oldalakat tölti le, és a legutóbbi jó adatokat lemezre menti, így újraindítás után is azonnal van adat

## Eszközök és entitások

Minden felvett bejegyzéshez (településhez) az alábbiak jönnek létre. A `<Név>` a beállításkor megadott név.

### `<Név> Forecast` – Weather forecast

| Entitás | Típus | Megjegyzés |
|---|---|---|
| `<Név> Forecast` | időjárás | aktuális állapot, hőmérséklet, szél; óránkénti és napi előrejelzés |
| Hőmérséklet | szenzor (°C) | aktuális |
| Állapot | szenzor (felsorolás) | pl. napos, borult, eső |
| Mai maximum hőmérséklet | szenzor (°C) | a napi előrejelzésből, éjfélig megmarad |
| Mai minimum hőmérséklet | szenzor (°C) | a napi előrejelzésből, éjfélig megmarad |
| Szélirány (fok) | szenzor (°) | az aktuális óra előrejelzéséből |
| Szélirány (égtáj) | szenzor (felsorolás) | 16 égtáj (É, ÉÉK, ÉK, …) |
| Szélerősség | szenzor (felsorolás) | Beaufort-skála 0–12 (Szélcsend … Orkán) |
| Szélsebesség | szenzor (km/h) | **becslés** a szélerősségből (az idokep.hu nem ad számot) |
| Vihar várható az elkövetkező órában | bináris szenzor | |
| Riasztás | bináris szenzor | bármilyen riasztás |
| Sárga / Narancs / Piros riasztás | bináris szenzor | szintenként |

### `<Név> Weather` – Current weather (csak állomáskóddal)

Egy vagy több idokep.hu időjárás-állomás (automata) mért adatai, egy eszközben. Több állomásnál minden érték a választott **összevonás módja** szerint áll össze:

- **Prioritás**: az első olyan állomás értéke a listában, amelynek van egy óránál frissebb mérése; a következő állomás pótolja, amit az előző nem mér, vagy ha épp nem érhető el.
- **Átlag**: a friss mérések átlaga (a szélirány szögként átlagolva).

Minden szenzor `stations` attribútuma mutatja, melyik állomás(ok)tól származik az érték. Azok az értékek, amelyeket egyik állomás sem mér, „nem elérhető” állapotúak; ha egyiknek sincs egy óránál frissebb mérése, „ismeretlen”. Ha egy állomás épp nem érhető el, a többi adatai jelennek meg.

| Entitás | Mértékegység |
|---|---|
| Hőmérséklet | °C |
| Páratartalom | % |
| Légnyomás | hPa |
| 24 órás csapadék | mm |
| Éjfél óta hullott csapadék | mm |
| Csapadékintenzitás | mm/h |
| Szélsebesség | km/h |
| Széllökés | km/h |
| Szélirány (fok) | ° |
| UV-B index | – |
| Globálsugárzás | W/m² |
| Vízhőmérséklet | °C |
| Mérés ideje | időbélyeg |

### `National Radar Image` – Radar szolgáltatás

Az első bejegyzés felvételekor automatikusan létrejön egy **Radar** bejegyzés egy kép entitással (**National radar image**). Ez az országos radar utolsó 5 képéből készült animált GIF (képenként 1 mp, az utolsón 2 mp). Ha törlöd, a következő újraindításkor vagy újratöltéskor újra létrejön; ha nem kell, inkább kapcsold ki.

## Telepítés

### HACS-on keresztül

1. Nyisd meg a **HACS**-t, kattints a jobb felső **⋮** gombra, és válaszd az **Egyéni tárolók** (Custom repositories) menüpontot.
2. Add meg a tároló címét: `https://github.com/ztarsoly/ha-idokep-weather-and-forecast`, típusnak válaszd az **Integráció** (Integration) lehetőséget.
3. Keresd meg az **Időkép Weather & Forecast** integrációt, és telepítsd.
4. Indítsd újra a Home Assistantot.

### Kézzel

1. Másold a `custom_components/idokep_weather_and_forecast` mappát a Home Assistant `config/custom_components/` mappájába.
2. Indítsd újra a Home Assistantot.

## Beállítás

**Beállítások → Eszközök és szolgáltatások → Integráció hozzáadása → Időkép Weather & Forecast**

| Mező | Kötelező | Leírás |
|---|---|---|
| Név | igen | A bejegyzés és az eszközök neve (pl. Otthon → „Otthon Forecast”, „Otthon Weather”) |
| Település | igen | A település neve úgy, ahogy az idokep.hu címében szerepel, pl. `Budapest` vagy `Debrecen-Józsa` (`https://www.idokep.hu/idojaras/Debrecen-Józsa`) |
| Időjárás-állomások kódjai | nem | Egy vagy több idokep.hu automata kódja fontossági sorrendben, pl. `jozsamet` (`https://www.idokep.hu/automata/jozsamet`) |
| Összevonás módja | nem | Prioritás (alapértelmezett) vagy Átlag – lásd fent |

Az állomáskódot az [idokep.hu/automata](https://www.idokep.hu/automata) oldalon találod: válaszd ki az állomást a listából, a kód a címben a `/automata/` utáni rész (a listában zárójelben is látszik).

Egy település csak egyszer vehető fel. Több települést külön bejegyzésként adhatsz hozzá.

### Módosítás később

A bejegyzés **Beállítás** (Configure) gombjával módosítható:

- **Település**: más településre vált; a bejegyzés és az eszközök neve, valamint az entitások azonosítói megmaradnak.
- **Időjárás-állomások kódjai**: állomások hozzáadása, törlése vagy sorrendjük módosítása; mind törölve a `<Név> Weather` eszköz megszűnik.
- **Összevonás módja**: Prioritás vagy Átlag.

A bejegyzés nevét a Home Assistant saját **Átnevezés** funkciójával lehet módosítani.

## Frissítési ütemezés

| Adatforrás | Mit frissít | Ütemezés | Hiba esetén |
|---|---|---|---|
| Aktuális időjárás (`/idojaras/`) | hőmérséklet, állapot | 10 percenként (x:00, x:10, x:20, …) | percenként újra |
| Előrejelzés (`/elorejelzes/`) | óránkénti és napi előrejelzés, mai max/min, szél, riasztások | óránként (x:00) | 10 percenként újra |
| Időjárás-állomások (`/automata/<kód>`) | állomás szenzorai | 5 percenként (x:00:59, x:05:59, …), minden állomás egyszerre | percenként újra, ha egyik sem érhető el |
| Radar (`/radar/`) | radarkép | 5 percenként (x:00:59, x:05:59, …) | percenként, amíg új kép nem érkezik |

- Indításkor (újraindítás, újratöltés, új bejegyzés) minden adat azonnal frissül.
- Csak az az oldal töltődik le, amelyet legalább egy bekapcsolt entitás használ.
- Ha a bejegyzés rendszerbeállításaiban kikapcsolod a lekérdezést (**Enable polling for updates**), az ütemezett frissítés leáll.

## Adattárolás

Az előrejelzési oldalak legutóbbi sikeres adatai a Home Assistant `config/.storage/idokep_weather_and_forecast.<bejegyzés azonosító>` fájljába kerülnek. Újraindítás után csak a még érvényes adatok töltődnek vissza (el nem múlt órák és napok, a mai max/min aznap, a többi adat egy órán belül), így az entitások akkor is kapnak értéket, ha az idokep.hu épp nem érhető el. A bejegyzés törlésekor a fájl is törlődik. Az állomás és a radar adatai nem kerülnek lemezre.

## Megjegyzések

- A szélsebesség-szenzor a Beaufort-fokozat középértékéből számolt **becslés**; valódi mért szélsebességet az időjárás-állomás ad.
- Este, amikor az idokep.hu már nem mutatja a mai napot, a mai napi előrejelzés és a mai max/min éjfélig megmarad.
- Az idokep.hu az óránkénti előrejelzést a következő órától listázza; az aktuális óra egy korábbi lekérdezésből megmarad, így az előrejelzés az aktuális órával kezdődik (közvetlenül az első telepítés után még a következő órával).
- Az állomásadatok az idokep.hu grafikonjai mögötti adatfájlból származnak, ami nem hivatalos felület, és megváltozhat.

## Köszönetnyilvánítás

Ez az integráció [FabianGabor](https://github.com/FabianGabor) [HA-Idokep](https://github.com/FabianGabor/HA-Idokep) integrációjára épül. Köszönet az eredeti munkáért!

Az időjárási adatok forrása az [Időkép](https://www.idokep.hu). Ez a projekt nem az Időkép hivatalos terméke.

## Licenc

[MIT](LICENSE)
