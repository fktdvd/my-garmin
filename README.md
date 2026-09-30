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

A projekt [uv](https://docs.astral.sh/uv/)-t használ; Windows-on és Linuxon ugyanúgy működik.
Python ≥ 3.12 kell, de az uv magától letölti (`.python-version`), a verziók az `uv.lock`-ban rögzítve vannak.

```bash
# uv telepítése
winget install astral-sh.uv                        # Windows
curl -LsSf https://astral.sh/uv/install.sh | sh    # Linux / macOS

# függőségek (.venv létrehozása)
uv sync --extra analysis        # elemzéssel (Jupyter, pandas, …)
uv sync                         # csak a szinkron (pl. szerveren)
```

## Használat

```bash
# 1. Egyszeri bejelentkezés (e-mail, jelszó, MFA ha van). A tokenek a data/.garminconnect alá kerülnek,
#    a jelszó NEM mentődik. Amíg a tokenek érvényesek (a sync frissíti őket), nem kell újra belépni.
#    Új gépen újra be kell lépni (vagy át kell másolni a data/ mappát).
uv run mygarmin login

# 2. Kézi szinkron (első futáskor az utolsó 30 nap, utána inkrementális)
uv run mygarmin sync

# Teljes visszatöltés egy adott naptól (lassú: napi ~30 hívás!)
uv run mygarmin sync --since 2024-01-01

# SQLite újraépítése a raw fájlokból
uv run mygarmin ingest --rebuild
```

Az adatmappa helye a `MYGARMIN_DATA_DIR` környezeti változóval módosítható (alapból `./data`).

### Ütemezés (01:00 és 13:00)

Windows (Feladatütemező):

```powershell
.\scripts\register-task.ps1                                       # regisztrálás (admin jog nem kell)
Unregister-ScheduledTask -TaskName my-garmin-sync -Confirm:$false  # törlés
```

Linux (cron):

```bash
./scripts/install-cron.sh             # telepítés (idempotens)
./scripts/install-cron.sh --remove    # törlés
SCHEDULE="0 */6 * * *" ./scripts/install-cron.sh   # más időzítés
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

## SQLite táblák

| tábla | forrás |
|---|---|
| `daily_summary` | lépés, kalória, nyugalmi/min/max pulzus, stressz, Body Battery, emelet, intenzív percek |
| `sleep` | alvásfázisok, alvás pontszám, légzés, SpO2 |
| `hrv` | éjszakai / heti HRV, státusz |
| `intraday` | napon belüli idősorok hosszú formában (`metric`, `ts_ms`, `value`): `heart_rate` (2 perc), `stress`, `body_battery` (3 perc), `respiration` |
| `training_readiness` | edzéskészség pontszám és tényezői; naponta több mérés, `is_morning=1` az ébredés utáni |
| `training_status` | edzésállapot, akut/krónikus terhelés, ACWR, VO2max, havi terheléseloszlás |
| `activities` | aktivitás összefoglalók + hivatkozás a raw mappára |
| `activity_laps` | körök / szakaszok (időtartam, pulzus, intenzitás típus: ACTIVE/REST/…) |
| `activity_samples` | aktivitás idősor (pulzus, sebesség, magasság, kadencia, Body Battery, GPS, …) |

Időbélyegek: `*_ms` = Unix epoch ms (UTC), `*_gmt` = UTC szöveg, `*_local` = az óra helyi ideje.
Minden más a `data/raw` alatt elérhető, és igény szerint bővíthető a `src/mygarmin/db.py`-ban.
Az eredeti FIT rekordok: `mygarmin.analysis.Activity.fit_messages()`.

```sql
-- példa: óránkénti átlagpulzus egy napon
SELECT strftime('%H', ts_ms / 1000, 'unixepoch', 'localtime') AS ora, ROUND(AVG(value)) AS pulzus
FROM intraday WHERE metric = 'heart_rate' AND date = '2026-09-20' GROUP BY ora;
```

## Elemzés

```bash
uv run jupyter lab notebooks
```

| notebook | tartalom |
|---|---|
| `01_overview.ipynb` | napi trendek, alvás, aktivitások összesítve |
| `02_activity.ipynb` | egy aktivitás részletesen: összefoglaló, edzéshatás, idősorok (pulzus, sebesség, magasság, kadencia, Body Battery), pulzuszónák, körök + pulzus-visszaállás pihenőkben, GPS útvonal, időjárás, eredeti FIT |

A notebookok a `mygarmin.analysis` modult használják (`load_activity`, `lap_stats`, …).
A kimenetek személyes adatot tartalmaznak — commit előtt töröld őket (*Clear All Outputs*).

## Megjegyzések

- A [python-garminconnect](https://github.com/cyberjunky/python-garminconnect) nem hivatalos kliens:
  Garmin-oldali változás bármikor eltörheti. A verzió a `pyproject.toml`-ban rögzítve van.
- A `data/` mappa személyes adatot és tokeneket tartalmaz — `.gitignore`-ban van, soha ne commitold.
- Tesztek: `uv run pytest`
