from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from mygarmin import db
from mygarmin.config import Config
from mygarmin.report import mail, runner
from mygarmin.report.agent import LLMConfig, ReadOnlySQL, run_agent
from mygarmin.report.facts import build_facts, has_data, period_for, previous
from mygarmin.report.profile import DEFAULT_PROFILE, load_profile

GOALS = DEFAULT_PROFILE["goals"]


def test_periods() -> None:
    wed = date(2026, 9, 30)
    d = period_for("daily", wed)
    assert (d.start, d.end, d.label) == (date(2026, 9, 29), date(2026, 9, 29), "2026-09-30")
    assert previous(d).end == date(2026, 9, 28)

    w = period_for("weekly", wed)
    assert (w.start, w.end, w.label) == (date(2026, 9, 21), date(2026, 9, 27), "2026-W39")
    assert (previous(w).start, previous(w).end) == (date(2026, 9, 14), date(2026, 9, 20))
    assert period_for("weekly", date(2026, 9, 28)).end == date(2026, 9, 27)  # Monday -> week just ended

    m = period_for("monthly", date(2026, 10, 1))
    assert (m.start, m.end, m.label, m.days) == (date(2026, 9, 1), date(2026, 9, 30), "2026-09", 30)
    assert previous(m).label == "2026-08"
    assert period_for("monthly", date(2026, 1, 15)).label == "2025-12"


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    cfg = Config(tmp_path)
    conn = db.connect(cfg)
    for i in range(1, 31):
        d = f"2026-09-{i:02d}"
        conn.execute("INSERT INTO sleep (date, total_s, score, deep_s, rem_s, awake_s, bedtime_local, wake_local) "
                     "VALUES (?, ?, ?, 3600, 5400, 900, ?, ?)",
                     (d, 25200 + i * 60, 60 + i, f"2026-09-{i - 1:02d}T23:{i:02d}:00" if i > 1 else None,
                      f"{d}T06:30:00"))
        conn.execute("INSERT INTO hydration (date, intake_ml, goal_ml, sweat_loss_ml) VALUES (?, ?, 2300, 300)",
                     (d, 0 if i % 5 == 0 else 1000 + i * 40))
        conn.execute("INSERT INTO daily_summary (date, steps, resting_hr, avg_stress) VALUES (?, ?, 50, 30)",
                     (d, 8000 + i))
        conn.execute("INSERT INTO training_readiness (date, timestamp_local, is_morning, score, sleep_pct, hrv_pct) "
                     "VALUES (?, ?, 1, ?, 60, 90)", (d, f"{d}T06:40:00", 50 + i))
        conn.execute("INSERT INTO training_readiness (date, timestamp_local, is_morning, score) VALUES (?, ?, 1, 1)",
                     (d, f"{d}T07:40:00"))
    conn.execute("INSERT INTO activities (activity_id, start_local, name, type, duration_s, aerobic_te, "
                 "training_load, hr_z2_s, hr_z3_s, raw_dir) "
                 "VALUES (1, '2026-09-29T06:24:00', 'Indoor Cycling', 'indoor_cycling', 1800, 2.5, 40, 900, 300, "
                 "'/secret/path')")
    conn.commit()
    conn.close()
    return cfg


def test_daily_facts(cfg: Config) -> None:
    f = build_facts(cfg.db_path, period_for("daily", date(2026, 9, 30)), GOALS)
    assert has_data(f)
    assert f["last_night"]["score"] == 90
    assert f["last_night"]["bedtime"] == "23:30"
    assert f["morning_readiness"]["score"] == 80  # earliest morning row wins
    assert f["morning_readiness"]["weakest_factors"][0]["pct"] == 60
    assert f["yesterday"]["activities"][0]["zone_min"] == [0, 15, 5, 0, 0]
    assert [d["date"] for d in f["last_7_days"]] == [f"2026-09-{i}" for i in range(23, 30)]
    json.dumps(f)


def test_monthly_facts(cfg: Config) -> None:
    f = build_facts(cfg.db_path, period_for("monthly", date(2026, 10, 1)), GOALS)
    h = f["this_month"]["hydration"]
    assert (h["days"], h["logged_days"], h["days_reached_goal"]) == (30, 24, 4)
    assert h["avg_ml_logged_days"] > h["avg_ml"]
    assert f["this_month"]["recovery"]["avg_readiness_morning"] == pytest.approx(65.5)
    assert f["this_month"]["training"]["aerobic_zone_min"] == 20
    assert "bedtime_vs_sleep_score" in f["correlations_spearman"]
    assert not has_data(build_facts(cfg.db_path, period_for("monthly", date(2026, 9, 1)), GOALS))


def test_readonly_sql(cfg: Config) -> None:
    sql = ReadOnlySQL(cfg.db_path, max_rows=5)
    out = json.loads(sql("SELECT activity_id, raw_dir FROM activities"))
    assert out["rows"] == [[1, None]]
    out = json.loads(sql("SELECT date FROM sleep"))
    assert len(out["rows"]) == 5 and out["truncated"]
    assert "error" in json.loads(sql("DELETE FROM sleep"))
    assert "error" in json.loads(sql("ATTACH DATABASE 'x.db' AS x"))
    assert json.loads(sql("SELECT count(*) FROM sleep"))["rows"] == [[30]]


class FakeClient:
    """Answers with one run_sql tool call, then with the final text."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=10)
        if len(self.calls) == 1:
            call = SimpleNamespace(id="c1", function=SimpleNamespace(
                name="run_sql", arguments=json.dumps({"query": "SELECT count(*) AS n FROM sleep"})))
            msg = SimpleNamespace(content=None, tool_calls=[call], model_extra={"reasoning_content": "hmm"})
        else:
            msg = SimpleNamespace(content="## Röviden\nMinden rendben.", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


def test_agent_loop(cfg: Config) -> None:
    client = FakeClient()
    res = run_agent(client, LLMConfig(api_key="x"), [{"role": "user", "content": "hi"}], ReadOnlySQL(cfg.db_path))
    assert res.text.startswith("## Röviden")
    assert res.queries == ["SELECT count(*) AS n FROM sleep"]
    assert (res.prompt_tokens, res.completion_tokens) == (200, 20)
    second = client.calls[1]["messages"]
    assert second[1]["reasoning_content"] == "hmm"
    assert json.loads(second[2]["content"])["rows"] == [[30]]


def test_agent_forces_final_answer(cfg: Config) -> None:
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if kwargs.get("tool_choice") == "none":
            msg = SimpleNamespace(content="vége", tool_calls=None)
        else:
            call = SimpleNamespace(id=f"c{len(calls)}", function=SimpleNamespace(
                name="run_sql", arguments=json.dumps({"query": "SELECT 1"})))
            msg = SimpleNamespace(content=None, tool_calls=[call])
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    res = run_agent(client, LLMConfig(api_key="x", max_tool_rounds=2), [], ReadOnlySQL(cfg.db_path))
    assert res.text == "vége" and len(res.queries) == 2 and len(calls) == 3


def test_generate_and_auto(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = []
    monkeypatch.setattr(runner, "send", lambda c, m: sent.append(m))
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("MAIL_TO", "a@example.com, b@example.com")
    llm = LLMConfig(api_key="x", model="fake")

    paths = runner.run_auto(cfg, date(2026, 10, 1), client=FakeClient(), llm=llm)
    # daily + weekly (W39, since W40 is incomplete: Sep 28 - Oct 4 has no full week yet) + September
    assert [p.parent.name for p in paths] == ["daily", "weekly", "monthly"]
    assert paths[2].name == "2026-09.md" and paths[2].with_suffix(".json").exists()
    assert len(sent) == 3 and sent[0]["To"] == "a@example.com, b@example.com"
    assert "fake · 1 SQL" in paths[0].read_text(encoding="utf-8")

    # second run the same day: nothing new
    assert runner.run_auto(cfg, date(2026, 10, 1), client=FakeClient(), llm=llm) == []


def test_mail_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SMTP_HOST", raising=False)
    assert mail.mail_config_from_env() is None
    c = mail.MailConfig("h", 587, None, None, "me@x", ["you@x"])
    msg = mail.build_message(c, "Tárgy", "## Cím\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    html = msg.get_body(("html",)).get_content()
    assert "<table>" in html and "<h2>Cím</h2>" in html
    assert msg.get_body(("plain",)).get_content().startswith("## Cím")


def test_profile_merge(tmp_path: Path) -> None:
    cfg = Config(tmp_path)
    (tmp_path / "profile.toml").write_text('[goals]\nhydration_ml = 2500\n[context]\ntext = "mászó"\n',
                                           encoding="utf-8")
    p = load_profile(cfg)
    assert p["goals"]["hydration_ml"] == 2500 and p["goals"]["sleep_hours"] == 7.5
    assert "mászó" in runner.system_prompt(p, "weekly")
    assert "2500 ml" in runner.system_prompt(p, "daily")


def test_example_profile_parses(tmp_path: Path) -> None:
    example = Path(__file__).parents[1] / "profile.example.toml"
    (tmp_path / "profile.toml").write_bytes(example.read_bytes())
    p = load_profile(Config(tmp_path))
    assert "swing" in p["context"]["text"] and p["report"]["max_words"]["daily"] == 250
