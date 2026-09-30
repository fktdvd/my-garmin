"""Generate daily / weekly / monthly AI reports: facts -> LLM -> Markdown file -> e-mail."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from mygarmin.config import Config
from mygarmin.db import SCHEMA
from mygarmin.report.agent import LLMConfig, ReadOnlySQL, llm_config_from_env, make_client, run_agent
from mygarmin.report.facts import Period, build_facts, has_data, period_for
from mygarmin.report.mail import build_message, mail_config_from_env, send
from mygarmin.report.profile import load_profile

log = logging.getLogger(__name__)

KINDS = ("daily", "weekly", "monthly")
KIND_HU = {"daily": "napi", "weekly": "heti", "monthly": "havi"}

GUIDE = """\
Értelmezési segédlet:
- Dátumkonvenció: az alvás, HRV és reggeli readiness D napon = a D reggelén véget ért éjszaka. Folyadék, lépés, stressz, edzés D napon = D nap.
- Folyadék: 0 ml valószínűleg azt jelenti, hogy aznap nem volt rögzítve, nem azt, hogy nem ivott. A Garmin cél az izzadással nő.
- Stressz (0-100): 0-25 pihenés, 26-50 alacsony, 51-75 közepes, 76-100 magas.
- Training effect (0-5): <1 nincs hatás, 1-2 kisebb, 2-3 fenntartó, 3-4 fejlesztő, 4-5 nagyon fejlesztő, 5 túlterhelő.
- Pulzuszónák: z1 bemelegítés, z2 aerob alap (ez építi leginkább az állóképességet), z3 tempó, z4 küszöb, z5 maximális.
- ACWR (akut/krónikus terhelés): 0,8-1,3 optimális, >1,5 sérülés- és túlterheléskockázat.
- Havi terhelésegyensúly (aerobic_low / aerobic_high / anaerobic) a Garmin célsávjaihoz viszonyítva; *_SHORTAGE = hiány.
- Readiness faktorok %-ban: minél alacsonyabb, annál inkább az a faktor húzza le a readinesst.
- A felhasználó mászik (ezt az óra nem méri), ezért az aerob kapacitást közvetett edzések (kerékpár, kettlebell) alapján nézzük.
"""

SECTIONS = {
    "daily": "## Röviden (1-2 mondat)\n## Alvás\n## Folyadék\n## Edzés és regeneráció\n## Mai javaslat (1-2 konkrét dolog)",
    "weekly": ("## Röviden\n## Célok (táblázat: cél | eredmény | ✅/⚠️)\n## Alvás\n## Folyadék\n"
               "## Aerob edzés (zónapercek, terhelésegyensúly)\n## Regeneráció\n## 3 lépés a jövő hétre"),
    "monthly": ("## Röviden\n## Hónap vs. előző hónap (táblázat)\n## Alvás\n## Folyadék\n"
                "## Aerob fejlődés (VO2max, zónák, terhelés)\n## Összefüggések és szokások (hét napjai, korrelációk)\n"
                "## Fókusz a következő hónapra (max. 3 pont)"),
}


def system_prompt(profile: dict[str, Any], kind: str) -> str:
    g = profile["goals"]
    lang = profile["report"]["language"]
    words = profile["report"]["max_words"][kind]
    ctx = (profile["context"].get("text") or "").strip()
    return f"""Személyes egészség- és edzéselemző asszisztens vagy. {KIND_HU[kind].capitalize()} riportot írsz {lang} nyelven egy Garmin-óra adataiból.

A felhasználó céljai:
- jobb alvás: legalább {g['sleep_hours']} óra, lefekvés kb. {g['bedtime']}-kor, egyenletes ritmus
- napi {g['hydration_ml']} ml folyadék
- aerob kapacitás javítása a mászáshoz: heti {g['aerobic_sessions_per_week']} aerob edzés, heti {g['aerobic_minutes_per_week']} perc z2-z3 zónában
{f"Háttér a felhasználótól: {ctx}" if ctx else ""}

Szabályok:
- Csak a kapott tényekre és a run_sql eszközzel lekérdezett adatokra támaszkodj; ne találj ki számot. Ha valami hiányzik, mondd ki.
- A megállapításokat számokkal támaszd alá (pl. "6,4 óra, a cél 7,5").
- Legfeljebb 3 konkrét, azonnal megvalósítható javaslat, a célokhoz kötve. Ne ismételd a triviálisat.
- Nem vagy orvos: nincs diagnózis. Korreláció nem ok-okozat; kevés adatnál légy óvatos.
- A run_sql eszközt csak akkor használd, ha a kapott tények nem elegek (pl. hosszabb trend kell).
- Formátum: Markdown, pontosan ezek a szakaszok:
{SECTIONS[kind]}
- Legfeljebb kb. {words} szó. Tömör, barátságos, tegeződő hangnem.

{GUIDE}
Adatbázis séma (SQLite, a run_sql-hez; a lat/lon oszlopok NULL-t adnak):
{SCHEMA}"""


def user_prompt(facts: dict[str, Any]) -> str:
    return ("Itt vannak az előre kiszámolt tények (JSON). Írd meg belőlük a riportot.\n\n"
            + json.dumps(facts, ensure_ascii=False, indent=1, default=str))


@dataclass
class ReportPaths:
    markdown: Path
    facts: Path


def report_paths(cfg: Config, p: Period) -> ReportPaths:
    base = cfg.data_dir / "reports" / p.kind
    return ReportPaths(base / f"{p.label}.md", base / f"{p.label}.json")


def subject(p: Period) -> str:
    return f"my-garmin {KIND_HU[p.kind]} riport – {p.label}"


def generate(cfg: Config, kind: str, today: date, *, dry_run: bool = False, send_email: bool = True,
             force: bool = False, client: Any = None, llm: LLMConfig | None = None) -> Path | None:
    p = period_for(kind, today)
    paths = report_paths(cfg, p)
    if paths.markdown.exists() and not force and not dry_run:
        log.info("%s riport már létezik: %s", kind, paths.markdown)
        return None

    profile = load_profile(cfg)
    facts = build_facts(cfg.db_path, p, profile["goals"])
    if not has_data(facts):
        log.warning("Nincs adat a(z) %s riporthoz (%s), kihagyva", kind, p.label)
        return None

    messages = [{"role": "system", "content": system_prompt(profile, kind)},
                {"role": "user", "content": user_prompt(facts)}]
    if dry_run:
        print(messages[0]["content"])
        print("\n" + "=" * 80 + "\n")
        print(messages[1]["content"])
        return None

    llm = llm or llm_config_from_env()
    client = client or make_client(llm)
    result = run_agent(client, llm, messages, ReadOnlySQL(cfg.db_path))
    log.info("%s riport kész: %d SQL, %d+%d token", kind, len(result.queries),
             result.prompt_tokens, result.completion_tokens)
    text = (f"{result.text}\n\n---\n<small>{llm.model} · {len(result.queries)} SQL-lekérdezés · "
            f"{result.prompt_tokens}+{result.completion_tokens} token</small>\n")

    paths.markdown.parent.mkdir(parents=True, exist_ok=True)
    paths.facts.write_text(json.dumps(facts, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    paths.markdown.write_text(text, encoding="utf-8")
    log.info("Mentve: %s", paths.markdown)

    if send_email:
        mail = mail_config_from_env()
        if mail is None:
            log.warning("Nincs SMTP_HOST / MAIL_TO beállítva, e-mail kihagyva")
        else:
            send(mail, build_message(mail, subject(p), text))
            log.info("E-mail elküldve: %s", ", ".join(mail.recipients))
    return paths.markdown


def run_auto(cfg: Config, today: date, *, send_email: bool = True, client: Any = None,
             llm: LLMConfig | None = None) -> list[Path]:
    """Daily for today, plus the last full week / month if not yet generated (catches up after downtime)."""
    done = []
    for kind in KINDS:
        path = generate(cfg, kind, today, send_email=send_email, client=client, llm=llm)
        if path:
            done.append(path)
    return done
