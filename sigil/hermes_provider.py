"""Sigil as a Hermes memory provider — ``memory.provider: sigil`` (the legacy ``quipu`` name still
resolves for one release).

The manager drives this class per agent instance (multiplexing contract, ``MemoryProvider``):
every store, path and thread is bound from ``initialize()`` and kept on the instance — no
module-level state. Recall goes straight to the sigil SQLite FTS index (no LLM, no network);
turn sync is an append-only JSONL per session, never a vault entry per turn — that per-turn
failure is exactly what sigil exists to prevent.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any

try:  # hermes runtime; sigil used standalone falls back so the package imports anywhere
    from agent.memory_provider import MemoryProvider, spawn_context_thread
except Exception:  # pragma: no cover - exercised only outside hermes

    class MemoryProvider:  # type: ignore[no-redef]
        """Fallback base so a standalone ``import sigil.hermes_provider`` never fails."""

    def spawn_context_thread(target, *, name: str, daemon: bool = True) -> threading.Thread:  # type: ignore[no-redef]
        return threading.Thread(target=target, name=name, daemon=daemon)


logger = logging.getLogger(__name__)

_CHAR_PER_TOKEN = 4      # rough budget for the ≤400-token recall cap
_DEFAULT_LIMIT = 5
_DEFAULT_MAX_TOKENS = 400
_MIN_QUERY_CHARS = 4

#: The provider name. It follows the registration: Hermes hands the entry-point name to `register()`,
#: so the tools and the system block carry whatever name the profile selected. Since 0.10.0 there is
#: exactly one name to select — the old `quipu` entry point is gone.
PROVIDER_NAME = "sigil"
_TURN_CAPTURE_CHARS = 4000
_JOIN_TIMEOUT = 5.0

_SAFE_SESSION_RE = re.compile(r"[^A-Za-z0-9._-]")


def _load_plugin_config() -> dict[str, Any]:
    """Read ``memory.quipu`` from the hermes profile config; {} when unavailable."""
    try:
        from hermes_cli.config import load_config

        memory = load_config().get("memory", {})
        block = (memory.get("sigil") or memory.get("quipu") or {}) if isinstance(memory, dict) else {}
        return dict(block) if isinstance(block, dict) else {}
    except Exception:
        return {}


def _load_submodule(name: str):
    """Import ``sigil.<name>``, or None if it does not import (is_available() checks index)."""
    try:
        return importlib.import_module(f".{name}", __package__)
    except Exception as exc:
        logger.debug("sigil.%s failed to import: %s", name, exc)
        return None


def _candidates(hermes_home: str = "") -> list[Path]:
    """Vault lookup order, new name first: SIGIL_VAULT|QUIPU_VAULT, <hermes_home>/sigil-vault|
    quipu-vault, ~/sigil-vault|~/quipu-vault.

    Both names resolve for one release: a profile whose `.env` still exports QUIPU_VAULT, or whose
    vault directory has not been renamed yet, must keep finding its memory instead of starting empty.
    """
    out: list[Path] = []
    for var in ("SIGIL_VAULT", "QUIPU_VAULT"):
        env = os.environ.get(var)
        if env:
            out.append(Path(env).expanduser())
    if hermes_home:
        home = Path(hermes_home).expanduser()
        out += [home / "sigil-vault", home / "quipu-vault"]
    out += [Path.home() / "sigil-vault", Path.home() / "quipu-vault"]
    return list(dict.fromkeys(out))


def _resolve_vault(hermes_home: str = "") -> Path | None:
    for cand in _candidates(hermes_home):
        if cand.is_dir():
            return cand
    return None


def _safe_session(session_id: str) -> str:
    return _SAFE_SESSION_RE.sub("_", session_id) or "anon"


def _db_path(vault: str | Path) -> Path:
    """A pre-rename vault keeps its index under `.quipu`; reading it there is the difference between
    answering from memory and answering from nothing."""
    root = Path(vault)
    meta = root / ".sigil" if (root / ".sigil").exists() else root / ".quipu"
    return meta / "index.db"


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}


def _str_prop(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _schema_for(schema: dict[str, Any], provider_name: str) -> dict[str, Any]:
    """The same tool under the provider's own name: the tools follow the registration."""
    out = dict(schema)
    out["name"] = schema["name"].replace(f"{PROVIDER_NAME}_", f"{provider_name}_", 1)
    return out


SEARCH_SCHEMA = _schema(
    "sigil_search",
    "Search the sigil memory vault for prior outcomes, decisions and measurements. "
    "Use before answering anything project-history shaped.",
    {"query": _str_prop("What to look for (keywords, not sentences)."),
     "limit": {"type": "integer", "description": "Maximum results (default 5)."}},
    ["query"],
)

WRITE_SCHEMA = _schema(
    "sigil_write",
    "Record one real outcome in the sigil vault: a decision with its reason, a measurement, or a "
    "root cause. summary is the retrieval handle; evidence is how a skeptic re-checks it.",
    {"summary": _str_prop("One-sentence statement of the fact."),
     "evidence": _str_prop("Numbers, paths or commands a skeptic could re-run."),
     "ask": _str_prop("Optional: the standalone question this entry answers."),
     "kind": _str_prop("Optional entry kind (default 'fact')."),
     "project": _str_prop("Optional project tag."),
     "agent": _str_prop("Optional agent name."),
     "importance": {"type": "number",
                    "description": "Optional 0-1 confidence. With evidence and importance at or above "
                                   "the vault's promote_threshold, the entry is written promoted."},
     "state": {"type": "string", "enum": ["raw", "promoted", "pruned"],
               "description": "Optional. Omit to let the evidence+importance gate decide; "
                              "'promoted' is refused (written raw) when evidence is missing."},
     "supersedes": {"type": "array", "items": {"type": "string"},
                    "description": "Optional entry ids this entry corrects. Their edge windows close, "
                                   "so a rebuild reproduces history instead of resurrecting it."},
     "edges": {"type": "array", "items": {"type": "string"},
               "description": "Optional graph edges as src:predicate:dst. Predicates are a CLOSED "
                              "vocabulary (causes, depends_on, implements, supersedes, "
                              "decided_against, measured_as, located_in, used_by, blocks, owned_by, "
                              "part_of, contradicts, learned_from) — an invented predicate "
                              "is reported in skipped_edges, never silently accepted."}},
    ["summary", "evidence"],
)


class SigilMemoryProvider(MemoryProvider):
    """MemoryProvider backed by a local sigil vault (index recall + JSONL turn capture).

    The instance takes the name it was REGISTERED under, so the identity the provider reports is
    the one Hermes selected it by (`memory.provider: <name>`), and the tools follow it.
    """

    def __init__(self, config: dict[str, Any] | None = None, name: str = PROVIDER_NAME):
        cfg = dict(config) if config is not None else _load_plugin_config()
        self._config = cfg
        self._name = name or PROVIDER_NAME
        # SIGIL_* is the name; QUIPU_* still resolves because a profile `.env` written before the
        # rename carries it (data compatibility, not branding — same rule as `.quipu/`).
        self._limit = int(cfg.get("limit") or os.environ.get("SIGIL_PREFETCH_LIMIT")
                          or os.environ.get("QUIPU_PREFETCH_LIMIT") or _DEFAULT_LIMIT)
        self._max_tokens = int(cfg.get("max_tokens") or os.environ.get("SIGIL_PREFETCH_MAX_TOKENS")
                               or os.environ.get("QUIPU_PREFETCH_MAX_TOKENS") or _DEFAULT_MAX_TOKENS)
        self._hermes_home = ""
        self._vault = ""
        self._session_id = ""
        self._stats_line = ""
        self._lock = threading.Lock()
        self._pending: dict[str, str] | None = None
        self._prefetch_thread: threading.Thread | None = None
        self._writer_thread: threading.Thread | None = None
        self._write_queue: queue.Queue = queue.Queue()

    # -- lifecycle -----------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def _search_tool(self) -> str:
        return f"{self._name}_search"

    @property
    def _write_tool(self) -> str:
        return f"{self._name}_write"

    def is_available(self) -> bool:
        """A vault directory exists and sigil/index.py imports; config/deps only, no network."""
        if _load_submodule("index") is None:
            return False
        return _resolve_vault(self._hermes_home) is not None

    def unavailable_reason(self) -> str:
        looked = ", ".join(str(c) for c in _candidates(self._hermes_home))
        if _load_submodule("index") is None:
            return f"{self._name}: sigil/index.py does not import (looked for a vault at: {looked})"
        return f"{self._name}: no vault directory found (looked for: {looked})"

    def initialize(self, session_id: str, **kwargs) -> None:
        """Bind vault + session on the instance; open nothing long-lived."""
        self._hermes_home = str(kwargs.get("hermes_home") or "")
        self._session_id = session_id
        vault = _resolve_vault(self._hermes_home)
        self._vault = str(vault) if vault else ""
        self._stats_line = ""
        if self._vault:
            self._writer_thread = threading.Thread(target=self._writer_loop,
                                                   name=f"{self._name}-sync", daemon=True)
            self._writer_thread.start()

    def shutdown(self) -> None:
        """Drain the sync queue and join workers — nothing left running."""
        writer = self._writer_thread
        self._writer_thread = None
        if writer is not None and writer.is_alive():
            self._write_queue.put(None)
            writer.join(timeout=_JOIN_TIMEOUT)
        prefetcher = self._prefetch_thread
        self._prefetch_thread = None
        if prefetcher is not None and prefetcher.is_alive():
            prefetcher.join(timeout=_JOIN_TIMEOUT)

    # -- prompt + recall -----------------------------------------------------

    def system_prompt_block(self) -> str:
        if not self._vault:
            return ""
        if not self._stats_line:
            self._stats_line = self._compute_stats_line()
        return (f"# Sigil memory\nVault: {self._vault}{self._stats_line}\n"
                "Recall before answering; write after a real outcome, never per turn. "
                f"Search with {self._search_tool}, record outcomes with {self._write_tool}.")

    def _compute_stats_line(self) -> str:
        index_mod = _load_submodule("index")
        if index_mod is None:
            return ""
        try:
            stats = index_mod.stats(_db_path(self._vault))
        except Exception as exc:
            logger.debug("sigil stats unavailable: %s", exc)
            return ""
        promoted = stats.get("by_state", {}).get("promoted", 0)
        return f" ({stats.get('entries', 0)} entries, {promoted} promoted)"

    def _recall(self, query: str, max_tokens: int | None = None) -> str:
        index_mod = _load_submodule("index")
        if index_mod is None or not self._vault:
            return ""
        if not query or len(query.strip()) < _MIN_QUERY_CHARS:
            return ""
        try:
            rows = index_mod.search(_db_path(self._vault), query, limit=self._limit)
        except Exception as exc:
            logger.warning("sigil prefetch search failed: %s", exc)
            return ""
        if not rows:
            return ""
        lines = ["## Sigil recall"]
        for row in rows:
            lines.append(f"- [{row.get('id')}] {row.get('summary')} "
                         f"({row.get('state')}, score {row.get('score', 0.0)})")
        body = "\n".join(lines)
        budget = self._max_tokens if max_tokens is None else max(1, int(max_tokens))
        cap = budget * _CHAR_PER_TOKEN
        if len(body) > cap:
            kept = body[:cap]
            cut = kept.rfind("\n")
            body = kept[:cut] if cut > 0 else kept
        return body

    def prefetch(self, query: str, *, session_id: str = "", max_tokens: int | None = None) -> str:
        """Formatted recall for this turn: the queued background result if fresh, else direct.

        `max_tokens` overrides the instance budget for this call only; the default comes from
        configuration (`max_tokens`, or `QUIPU_PREFETCH_MAX_TOKENS`, or 400).
        """
        try:
            with self._lock:
                pending, self._pending = self._pending, None
            if pending and pending["query"] == query and pending["result"]:
                return pending["result"]
            return self._recall(query, max_tokens)
        except Exception as exc:
            logger.warning("sigil prefetch failed: %s", exc)
            return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Recall in the background; prefetch() consumes the cached result next turn."""

        def work() -> None:
            result = ""
            try:
                result = self._recall(query)
            except Exception as exc:
                logger.debug("sigil background prefetch failed: %s", exc)
            with self._lock:
                self._pending = {"query": query, "result": result}

        try:
            self._prefetch_thread = _spawn_thread(work, name=f"{self._name}-prefetch")
        except Exception as exc:
            logger.debug("sigil queue_prefetch dropped: %s", exc)

    # -- turn sync -----------------------------------------------------------

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages: list[dict[str, Any]] | None = None,
                  turn_author: dict[str, Any] | None = None) -> None:
        """Append one JSON line per session transcript; never a vault entry per turn; never raises."""
        try:
            if not self._vault:
                return
            sid = _safe_session(session_id or self._session_id)
            self._write_queue.put(("turn", {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "session": sid,
                "user": (user_content or "")[:_TURN_CAPTURE_CHARS],
                "assistant": (assistant_content or "")[:_TURN_CAPTURE_CHARS],
            }))
        except Exception as exc:
            logger.debug("sigil sync_turn dropped: %s", exc)

    def _writer_loop(self) -> None:
        while True:
            item = self._write_queue.get()
            if item is None:
                return
            _, record = item
            try:
                path = _db_path(self._vault).parent / f"session-{record['session']}.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception as exc:
                logger.debug("sigil sync append failed: %s", exc)

    # -- tools ---------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [_schema_for(SEARCH_SCHEMA, self._name), _schema_for(WRITE_SCHEMA, self._name)]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        try:
            return self._handle_tool(tool_name, dict(args or {}))
        except Exception as exc:
            logger.warning("sigil tool %s failed: %s", tool_name, exc)
            return json.dumps({"error": f"{self._name} {tool_name} failed: {exc}"})

    def _handle_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        # exactly the two tools this provider advertises: a stale `quipu_*` schema is a stale session,
        # and answering it would hide that from the agent holding it
        if tool_name == self._search_tool:
            return self._tool_search(args)
        if tool_name == self._write_tool:
            return self._tool_write(args)
        return json.dumps({"error": f"{self._name} provider handles {self._search_tool}/"
                                      f"{self._write_tool}, not {tool_name!r}"})

    def _tool_search(self, args: dict[str, Any]) -> str:
        index_mod = _load_submodule("index")
        if index_mod is None or not self._vault:
            return json.dumps({"results": [], "error": "sigil vault not available"})
        query = str(args.get("query") or "").strip()
        if not query:
            return json.dumps({"error": "query is required"})
        try:
            limit = int(args.get("limit") or self._limit)
        except (TypeError, ValueError):
            limit = self._limit
        rows = index_mod.search(_db_path(self._vault), query, limit=limit)
        results = [{"id": r.get("id"), "summary": r.get("summary"), "state": r.get("state"),
                    "score": r.get("score", 0.0)} for r in rows]
        return json.dumps({"results": results})

    def _tool_write(self, args: dict[str, Any]) -> str:
        config_mod = _load_submodule("config")
        mcp_mod = _load_submodule("mcp")
        if config_mod is None or mcp_mod is None or not self._vault:
            return json.dumps({"error": "sigil vault not available"})
        if not str(args.get("summary") or "").strip():
            return json.dumps({"error": "summary is required"})
        if not str(args.get("evidence") or "").strip():
            return json.dumps({"error": "evidence is required"})
        payload = dict(args)
        payload.setdefault("kind", "fact")
        payload.setdefault("session", self._session_id)
        try:
            cfg = config_mod.load(self._vault)
        except config_mod.VaultNotFound as exc:
            # a write must not mint a fresh vault at a typo'd path (#141) — return the reason instead
            return json.dumps({"error": str(exc)})
        evidence = str(payload.get("evidence") or "").strip()
        try:
            importance = float(payload.get("importance") or 0.0)
        except (TypeError, ValueError):
            importance = 0.0
        payload["importance"] = importance
        requested = str(payload.get("state") or "").strip().lower()
        # An agent write is promoted only on the same grounds as any other entry: evidence AND
        # importance clearing the vault's threshold. `state: promoted` cannot mint one without them,
        # and an explicit `promoted` with no evidence is downgraded rather than refused, because
        # losing the memory to punish the caller is the worse trade.
        eligible = bool(evidence) and importance >= cfg.promote_threshold
        if requested == "promoted":
            state = "promoted" if eligible else "raw"
        elif requested in {"raw", "pruned"}:
            state = requested
        else:
            state = "promoted" if eligible else "raw"
        payload["state"] = state
        result = mcp_mod._write(cfg, payload)
        # The graph is unreachable from the surface agents actually write through unless the tool
        # carries edges, and the closed vocabulary is what keeps an invented predicate out (#84).
        out = {"id": result["id"], "path": result["path"], "state": state,
               "importance": importance, "skipped_edges": result.get("skipped_edges") or [],
               "closed_edges": result.get("closed_edges") or []}
        if requested == "promoted" and state != "promoted":
            out["note"] = (
                f"written raw, not promoted: promotion needs evidence and importance >= "
                f"{cfg.promote_threshold:g}, got evidence={'yes' if evidence else 'no'}, "
                f"importance={importance:g}"
            )
        return json.dumps(out)


def _spawn_thread(target, *, name: str) -> threading.Thread:
    """Daemon thread under the caller's contextvars where hermes provides the helper."""
    try:
        thread = spawn_context_thread(target, name=name)  # type: ignore[unresolved-name]
    except Exception:
        thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread


def register(ctx) -> None:
    """Entry point for the ``hermes_agent.memory_providers`` group.

    The provider takes the name it was REGISTERED under: Hermes resolves a provider by entry-point
    name and hands that name to the plugin context, so the provider — and the tools it exposes —
    carry the name the profile selected.
    """
    name = getattr(ctx, "name", "") or PROVIDER_NAME
    ctx.register_memory_provider(SigilMemoryProvider(name=name))