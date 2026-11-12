"""`sigil hook` — the session-end entry point a framework calls, without embedding sigil (#3).

A memory system that nobody calls gathers nothing, and a hook that fails quietly is worse than no hook:
the session ends, the framework says "memory: ok", and nothing was written. This module is the one place
that trade is decided, so every failure path raises or exits non-zero, and a hung endpoint cannot hold
the session open:

- `run()` does capture -> reflect -> apply in that order, writing only after the model returned, so a
  transport error leaves the vault untouched rather than half-written;
- the reflection call is bounded by `timeout` (default 300 s) on a worker thread, because a blocking
  `urlopen` has no deadline of its own once it is inside a framework's shutdown path;
- `dry_run` (or `QUIPU_HOOK_DRY_RUN=1`) returns the exact prompt and writes nothing, which is how a
  human checks the hook without spending tokens.

The transcript is a path to the session file (Hermes JSONL or plain markdown) or `-` for stdin.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import graph as graph_mod
from . import index as index_mod
from . import promote as promote_mod
from .store import Entry, iter_entries

DEFAULT_TIMEOUT = 300.0


class HookError(RuntimeError):
    """Anything that means the pass did not complete. The caller exits non-zero and prints it."""


@dataclass
class HookResult:
    written: list[Entry] = field(default_factory=list)
    prompt: str = ""
    proposed: int = 0
    chunks: int = 0
    resumed: int = 0
    seconds: float = 0.0
    skipped_edges: list[str] = field(default_factory=list)

    @property
    def promoted(self) -> int:
        return sum(1 for e in self.written if e.state == "promoted")

    def line(self) -> str:
        chunks = f" in {self.chunks} chunks" if self.chunks > 1 else ""
        resumed = f" ({self.resumed} resumed)" if self.resumed else ""
        return (f"{len(self.written)} entries, {self.promoted} promoted, "
                f"{len(self.written) - self.promoted} raw{chunks}{resumed}, {self.seconds:.1f}s")


def read_transcript(source: str | Path, stdin=None) -> str:
    """A path, or `-` for stdin, read through the shared loader (#2).

    The loader turns a Hermes session JSONL into `role: text` turns and passes markdown through
    untouched, so the hook and `reflect` read the same transcript the same way — and a session file
    reaches the model as turns, not as raw JSON lines. The loader raises `ValueError`; `run` speaks
    it as a `HookError`, so the hook still has exactly one exception type.
    """
    if str(source) == "-":
        return promote_mod.load_transcript((stdin or sys.stdin).read())
    # NOT `Path(source)`: a rendered transcript (--session-db) is text, and wrapping it in a Path
    # turned the first turn into a filename — "transcript not found: user: why did the box …" (#101)
    return promote_mod.load_transcript(source)


def served_model(base_url: str, timeout: float = 3.0) -> str:
    """Ask the endpoint which model it is actually serving.

    `[llm] model` defaults to the literal string "local", which names no weights: a vault configured
    that way would record "local" on every entry and the provenance field would be a placeholder. The
    OpenAI-compatible `/models` route answers with the alias the server loaded (#110), so ask it once
    per pass; any failure falls back to the configured name rather than blocking the write.
    """
    import json as _json
    import urllib.request

    url = f"{base_url.rstrip('/')}/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = _json.loads(r.read()).get("data") or []
        for item in data:
            name = str((item or {}).get("id") or "").strip()
            if name:
                return name
    except Exception:  # noqa: BLE001 - a naming lookup must never fail a pass
        return ""
    return ""


def _existing(cfg) -> list[str]:
    return [e.summary for _p, e in iter_entries(cfg.root) if e.state == "promoted"]


def _ledger_path(cfg, key: str) -> Path:
    """Where a transcript's finished chunks are recorded.

    Derived state, like the index: deleting it costs a re-read and nothing else. Keyed by the session
    id when there is one, so a session that grows between attempts keeps its record.
    """
    return cfg.meta_dir / "chunks" / f"{key}.json"


def _load_ledger(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_ledger(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=0, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _reflect_streaming(chunks: list[str], cfg, provider, timeout: float, apply_chunk,
                       key: str, progress: dict) -> None:
    """Read one chunk, apply it, record it — then the next. On a worker thread, bounded by `timeout`.

    The pass used to collect every chunk's proposals and apply them once at the end, so a failure on
    chunk 3 of 24 discarded the work of chunks 1 and 2 (#111). Measured: two 24-chunk sessions failed
    and lost everything twice, and a 70-chunk session would have lost 19 chunks per attempt.

    So the unit of work is the chunk: it is applied and recorded before the next request goes out, and
    a retry skips the chunks whose text has not changed. `urlopen(timeout=...)` covers a dead socket,
    not a server that accepted the request and stopped talking, which is why the whole loop still sits
    on a thread with a deadline of its own: a hung endpoint must not hold a session open.
    """
    box: dict = {"failed": None}
    stop = threading.Event()
    ledger_file = _ledger_path(cfg, key)
    done = _load_ledger(ledger_file)

    def work() -> None:
        existing = _existing(cfg)
        # chunks land as they finish now, so the cross-chunk guard lives here: a claim an earlier
        # chunk already wrote must not be written again by a later one (#111, and the 8-chunk fixture
        # that caught it the first time)
        claimed = {promote_mod.claim_key(s) for s in existing}
        index = 0
        try:
            for index, chunk in enumerate(chunks):
                if stop.is_set():
                    return
                digest = hashlib.sha256(chunk.encode("utf-8")).hexdigest()[:16]
                prior = done.get(str(index)) or {}
                if prior.get("sha") == digest:
                    progress["resumed"] += 1
                    continue
                proposals = [p for p in promote_mod.dedupe_proposals(
                    promote_mod.reflect(chunk, cfg, provider, existing)
                ) if promote_mod.claim_key(p.summary) not in claimed]
                if not proposals:
                    written = []
                else:
                    written = apply_chunk(proposals)
                progress["written"].extend(written)
                progress["applied"] += 1
                existing.extend(e.summary for e in written if e.state == "promoted")
                claimed.update(promote_mod.claim_key(e.summary) for e in written)
                done[str(index)] = {"sha": digest, "entries": [e.id for e in written]}
                _save_ledger(ledger_file, done)
        except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
            box["failed"] = index
            box["error"] = exc

    thread = threading.Thread(target=work, name="quipu-hook-reflect", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        stop.set()  # the in-flight chunk is discarded rather than applied after we have reported
        raise HookError(
            f"reflection timed out after {timeout:g}s (endpoint {cfg.llm_base_url}, "
            f"{len(chunks)} chunk{'s' if len(chunks) != 1 else ''}; "
            f"{progress['applied']} chunk(s) already applied, {len(progress['written'])} entries written)")
    if "error" in box:
        exc = box["error"]
        where = f" at chunk {(box.get('failed') or 0) + 1} of {len(chunks)}" if len(chunks) > 1 else ""
        raise HookError(
            f"reflection failed{where}: {type(exc).__name__}: {exc} "
            f"({progress['applied']} chunk(s) applied, {len(progress['written'])} entries written)") from exc


def run(cfg, transcript: str | Path, provider, agent: str = "", project: str = "",
        session: str = "", timeout: float = DEFAULT_TIMEOUT, dry_run: bool = False,
        stdin=None, max_chars: int = 24000) -> HookResult:
    started = time.monotonic()
    try:
        text = read_transcript(transcript, stdin=stdin)
    except ValueError as exc:  # the loader speaks ValueError; the hook speaks HookError
        raise HookError(str(exc)) from exc
    chunks = promote_mod.chunk_transcript(text, max_chars)

    if dry_run:
        # the first chunk is the first request the endpoint would receive
        return HookResult(prompt=promote_mod.build_prompt(chunks[0] if chunks else text, cfg,
                                                           _existing(cfg)),
                          seconds=time.monotonic() - started)
    # which model answered, recorded on every entry it produces: the endpoint's own name when it
    # reports one, else whatever the vault is configured to call (#110)
    model_name = str(getattr(provider, "model", "") or "")
    if not model_name or model_name == "local":
        # "local" is the config's placeholder, not a model name
        model_name = served_model(cfg.llm_base_url) or model_name or cfg.llm_model
    key = session or hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    report: dict = {}
    progress = {"written": [], "applied": 0, "resumed": 0}

    def apply_chunk(proposals):  # noqa: ANN001 - one chunk's proposals, already deduped
        return promote_mod.apply(proposals, cfg, agent=agent, project=project, session=session,
                                 source=str(transcript), report=report, model=model_name)

    try:
        _reflect_streaming(chunks, cfg, provider, timeout, apply_chunk, key, progress)
    except HookError:
        # whatever landed before the failure is real: index it, then say what happened
        graph_mod.materialize(cfg)
        index_mod.reindex(cfg.root, cfg.db_path)
        raise
    graph_mod.materialize(cfg)
    index_mod.reindex(cfg.root, cfg.db_path)
    return HookResult(written=progress["written"], proposed=len(progress["written"]),
                      chunks=len(chunks), resumed=progress["resumed"],
                      seconds=time.monotonic() - started,
                      skipped_edges=report.get("skipped_edges") or [])


def dry_run_requested(env: dict | None = None) -> bool:
    # SIGIL_HOOK_DRY_RUN is the name; QUIPU_HOOK_DRY_RUN still works because a deployed hook script or a
    # framework's env block carries it, and a dry run that silently becomes a spend is the wrong failure.
    source = env or os.environ
    value = source.get("SIGIL_HOOK_DRY_RUN") or source.get("QUIPU_HOOK_DRY_RUN", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}
