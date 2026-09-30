"""Minimal tool-calling agent loop over any OpenAI-compatible API (DeepSeek, OpenRouter, ...)."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class MissingConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    max_tool_rounds: int = 6
    temperature: float = 0.4


def llm_config_from_env() -> LLMConfig:
    key = os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not key:
        raise MissingConfigError("Nincs API-kulcs: állítsd be a DEEPSEEK_API_KEY-t (vagy LLM_API_KEY-t) a .env-ben")
    return LLMConfig(
        api_key=key,
        base_url=os.getenv("LLM_BASE_URL") or LLMConfig.base_url,
        model=os.getenv("LLM_MODEL") or LLMConfig.model,
    )


def make_client(cfg: LLMConfig) -> Any:
    from openai import OpenAI

    return OpenAI(api_key=cfg.api_key, base_url=cfg.base_url, timeout=120)


class ReadOnlySQL:
    """SELECT-only access to the DB; location and file path columns read as NULL."""

    HIDDEN_COLUMNS = {"lat", "lon", "raw_dir"}
    _ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                getattr(sqlite3, "SQLITE_RECURSIVE", 33)}

    def __init__(self, db_path: Path, max_rows: int = 200):
        self.db_path = db_path
        self.max_rows = max_rows

    def _authorize(self, action: int, arg1: Any, arg2: Any, _db: Any, _src: Any) -> int:
        if action == sqlite3.SQLITE_READ and arg2 in self.HIDDEN_COLUMNS:
            return sqlite3.SQLITE_IGNORE
        return sqlite3.SQLITE_OK if action in self._ALLOWED else sqlite3.SQLITE_DENY

    def __call__(self, query: str) -> str:
        conn = sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True)
        conn.set_authorizer(self._authorize)
        try:
            cur = conn.execute(query)
            cols = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(self.max_rows + 1)
        except sqlite3.Error as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)
        finally:
            conn.close()
        return json.dumps({"columns": cols, "rows": rows[: self.max_rows],
                           "truncated": len(rows) > self.max_rows}, ensure_ascii=False, default=str)


RUN_SQL_TOOL = {
    "type": "function",
    "function": {
        "name": "run_sql",
        "description": "Csak olvasható SQLite lekérdezés a Garmin adatbázison (SELECT). Max. 200 sor.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "SQLite SELECT utasítás"}},
            "required": ["query"],
        },
    },
}


@dataclass
class AgentResult:
    text: str
    queries: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0


def _assistant_message(msg: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"role": "assistant", "content": msg.content or "",
                           "tool_calls": [{"id": c.id, "type": "function",
                                           "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                          for c in msg.tool_calls]}
    # DeepSeek thinking models expect their reasoning back within a tool-calling turn.
    reasoning = getattr(msg, "reasoning_content", None) or (getattr(msg, "model_extra", None) or {}).get(
        "reasoning_content")
    if reasoning:
        out["reasoning_content"] = reasoning
    return out


def run_agent(client: Any, cfg: LLMConfig, messages: list[dict[str, Any]], sql: ReadOnlySQL | None) -> AgentResult:
    result = AgentResult(text="")
    messages = list(messages)
    for round_ in range(cfg.max_tool_rounds + 1):
        kwargs: dict[str, Any] = {"model": cfg.model, "messages": messages, "temperature": cfg.temperature}
        if sql is not None:
            kwargs["tools"] = [RUN_SQL_TOOL]
            if round_ == cfg.max_tool_rounds:
                kwargs["tool_choice"] = "none"  # force a final answer
        resp = client.chat.completions.create(**kwargs)
        usage = getattr(resp, "usage", None)
        if usage is not None:
            result.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
            result.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
        msg = resp.choices[0].message
        calls = getattr(msg, "tool_calls", None) or []
        if not calls or sql is None:
            result.text = (msg.content or "").strip()
            return result
        messages.append(_assistant_message(msg))
        for call in calls:
            try:
                query = json.loads(call.function.arguments or "{}").get("query", "")
            except json.JSONDecodeError:
                query = ""
            if call.function.name == "run_sql" and query:
                log.info("AI SQL: %s", " ".join(query.split())[:300])
                result.queries.append(query)
                output = sql(query)
            else:
                output = json.dumps({"error": f"ismeretlen eszköz vagy üres lekérdezés: {call.function.name}"})
            messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
    return result
