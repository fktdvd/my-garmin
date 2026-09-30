# my-garmin

Garmin Connect adatok lokális mentése, tárolása és elemzése — hivatalos API-kulcs nélkül.

Jelenlegi fókusz a **passzív ág** (#1): **#3** Garmin Connect → Local, **#4** Local → Értelmezés/vizualizáció.

```
Garmin Connect ──(mygarmin sync)──▶ data/raw/  (nyers JSON + eredeti FIT, változatlanul)
                                        │
                                  (mygarmin ingest)
                                        ▼
                                  data/garmin.db (SQLite) ──▶ notebooks/ (Jupyter), később Grafana
```

## Telepítés

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[analysis,dev]"
```

## Használat

```powershell
# 1. Egyszeri bejelentkezés (e-mail, jelszó, MFA ha van). A tokenek a data/.garminconnect alá kerülnek,
#    a jelszó NEM mentődik. Amíg a tokenek érvényesek (a sync frissíti őket), nem kell újra belépni.
.\.venv\Scripts\mygarmin login

# 2. Kézi szinkron (első futáskor az utolsó 30 nap, utána inkrementális)
.\.venv\Scripts\mygarmin sync

# Teljes visszatöltés egy adott naptól (lassú: napi ~30 hívás!)
.\.venv\Scripts\mygarmin sync --since 2024-01-01

# SQLite újraépítése a raw fájlokból
.\.venv\Scripts\mygarmin ingest --rebuild
```

### Ütemezés (01:00 és 13:00)

```powershell
.\scripts\register-task.ps1                                       # regisztrálás (admin jog nem kell)
Unregister-ScheduledTask -TaskName my-garmin-sync -Confirm:$false  # törlés
```

Ha a gép ki van kapcsolva, a futás kimarad; a következő (kézi vagy ütemezett) futás pótolja,
mert mindig az utolsó sikeres naptól tölt.

## Hogyan működik a szinkron

- **Napi végpontok** (alvás, pulzus, stressz, Body Battery, HRV, SpO2, légzés, edzéskészség, …) naponta:
  `data/raw/daily/<végpont>/<YYYY-MM-DD>.json`
- **Profil jellegű adatok** (eszközök, rekordok, zónák, felszerelés, …) futásonként egy pillanatkép:
  `data/raw/profile/<végpont>/<YYYY-MM-DD>.json`
- **Aktivitások**: részletek, splitek, időjárás, pulzuszónák + **eredeti FIT** (`original.zip`):
  `data/raw/activities/<id>/`. Egy már letöltött aktivitást nem tölt le újra.
- Az utolsó szinkronizált napot mindig újratölti (a mai nap adatai napközben még változnak).
- Hibás végpont nem állítja meg a futást; a sikertelen tételeket a következő futás újrapróbálja.
  A „nincs adat” (404) válasz nem hiba (pl. az órád nem mér SpO2-t).
- Garmin rate limit esetén a futás megáll, az állapot nem lép előre.
- Végpont ki/bekapcsolása: `src/mygarmin/endpoints.py` (`enabled=False`).

Állapot: `data/state.json`, napló: `data/logs/mygarmin.log`.

## SQLite táblák (első kör)

| tábla | forrás |
|---|---|
| `daily_summary` | lépés, kalória, nyugalmi/min/max pulzus, stressz, Body Battery, emelet, intenzív percek |
| `sleep` | alvásfázisok, alvás pontszám, légzés, SpO2 |
| `hrv` | éjszakai / heti HRV, státusz |
| `activities` | aktivitás összefoglalók + hivatkozás a raw mappára |

Minden más a `data/raw` alatt elérhető, és igény szerint bővíthető a `src/mygarmin/db.py`-ban.

## Elemzés

```powershell
.\.venv\Scripts\jupyter lab notebooks
```

## Megjegyzések

- A [python-garminconnect](https://github.com/cyberjunky/python-garminconnect) nem hivatalos kliens:
  Garmin-oldali változás bármikor eltörheti. A verzió a `pyproject.toml`-ban rögzítve van.
- A `data/` mappa személyes adatot és tokeneket tartalmaz — `.gitignore`-ban van, soha ne commitold.
- Tesztek: `.\.venv\Scripts\python -m pytest`
