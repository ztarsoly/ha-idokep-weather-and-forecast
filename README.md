# Időkép Weather & Forecast Home Assistant integráció

Egyéni (custom) Home Assistant integráció, amely az [idokep.hu](https://www.idokep.hu) adatait hozza be: aktuális időjárást, óránkénti és napi előrejelzést, időjárási riasztásokat, egy választható időjárás-állomás (automata) mért adatait és az országos radarképet.

Az integráció az idokep.hu nyilvános oldalait olvassa (nincs hivatalos API, felhasználói fiók sem kell). Ha az idokep.hu megváltoztatja az oldalait, az integrációt is frissíteni kell.

## Funkciók

- **Időjárás entitás** óránkénti és napi (kb. 23 napos) előrejelzéssel, a Home Assistant új forecast API-jával
- **Előrejelzési szenzorok**: hőmérséklet, állapot, mai maximum/minimum, szélirány, szélerősség, becsült szélsebesség
- **Riasztások**: vihar a következő órában, általános, sárga, narancs és piros riasztás
- **Időjárás-állomás** (nem kötelező): egy idokep.hu automata 12 mért adata és a mérés ideje
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

Egy idokep.hu időjárás-állomás (automata) mért adatai. Azok az értékek, amelyeket az adott állomás nem mér, „nem elérhető” állapotúak; az egy óránál régebbi mérés „ismeretlen”.

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
| Időjárás-állomás kódja | nem | Egy idokep.hu automata kódja, pl. `jozsamet` (`https://www.idokep.hu/automata/jozsamet`) |

Az állomáskódot az [idokep.hu/automata](https://www.idokep.hu/automata) oldalon találod: válaszd ki az állomást a listából, a kód a címben a `/automata/` utáni rész (a listában zárójelben is látszik).

Egy település csak egyszer vehető fel. Több települést külön bejegyzésként adhatsz hozzá.

### Módosítás később

A bejegyzés **Beállítás** (Configure) gombjával módosítható:

- **Település**: más településre vált; a bejegyzés és az eszközök neve, valamint az entitások azonosítói megmaradnak.
- **Időjárás-állomás kódja**: másik állomásra vált; üresre állítva a `<Név> Weather` eszköz törlődik.

A bejegyzés nevét a Home Assistant saját **Átnevezés** funkciójával lehet módosítani.

## Frissítési ütemezés

| Adatforrás | Mit frissít | Ütemezés | Hiba esetén |
|---|---|---|---|
| Aktuális időjárás (`/idojaras/`) | hőmérséklet, állapot | negyedóránként (x:00, x:15, x:30, x:45) | percenként újra |
| Előrejelzés (`/elorejelzes/`) | óránkénti és napi előrejelzés, mai max/min, szél, riasztások | óránként (x:00) | 10 percenként újra |
| Időjárás-állomás (`/automata/<kód>`) | állomás szenzorai | 5 percenként (x:00:59, x:05:59, …) | percenként újra |
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

## Fejlesztés

A tesztek a `tests/` mappában vannak:

```bash
pip install -r requirements.txt -r test-requirements.txt
pytest
```

A `requirements.txt` a Home Assistant 2025.12.5 verziót telepíti, a tesztek ezzel futnak.

A kód stílusát a [ruff](https://docs.astral.sh/ruff/) ellenőrzi (`ruff check .` és `ruff format .`).

Hibát vagy javaslatot az [Issues](https://github.com/ztarsoly/ha-idokep-weather-and-forecast/issues) oldalon jelezhetsz.

## Köszönetnyilvánítás

Ez az integráció [FabianGabor](https://github.com/FabianGabor) [HA-Idokep](https://github.com/FabianGabor/HA-Idokep) integrációjára épül. Köszönet az eredeti munkáért!

Az időjárási adatok forrása az [Időkép](https://www.idokep.hu). Ez a projekt nem az Időkép hivatalos terméke.

## Licenc

[MIT](LICENSE)
