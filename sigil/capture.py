"""Reflect finished Hermes sessions into the vault, one pass per session.

Hermes keeps live session transcripts in each profile's `state.db`; `sessions/*.jsonl` is only
written for exports, so anything reading that directory reflects the past and nothing else (#101).

A session is a candidate when its prose is worth reading and it is finished:

  * at least `--min-prose` bytes of user+assistant text (a "Reply exactly OK" turn is not memory),
  * idle for at least `--min-age` minutes (a running session is still changing),
  * younger than `--max-age-days`,
  * and no entry in the vault already carries its id — unless the session has GROWN since that pass by
    `--regrow` (default 1.25x), because a session is only read once otherwise: the session this rule
    came from was captured at 28 entries with 48 KB of prose and then ran to 191 KB, and everything
    after the capture point was never read (#126). The hook deletes its chunk ledger when a pass
    completes, so re-reading a finished session is a FULL pass, not just the tail — measured 868.7 s
    for 253 KB. Capped at `--regrow-per-day` re-passes per session (default 2) so a chatty session
    cannot own the timer.

Each pass is `hook.run`, so the failure contract is the hook's: a pass that reaches nothing raises,
is logged with its message, and never looks like success. Local endpoint only — this runs on a timer
and must not spend on a cloud model.

Before anything else each run asks the audit whether the index still agrees with the markdown, and
reindexes if it does not: a client holding older code can blank derived columns that every other
surface never reads (#128), and a fifteen-minute timer is a better repair interval than a human.

    sigil-capture --list                   # candidates, run nothing
    sigil-capture --limit 3                # reflect up to 3 (the timer's normal mode)
    sigil-capture --min-prose 5000 --all   # a one-off backfill over everything in the window
"""
from __future__ import annotations

import argparse
import fcntl
import json
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from . import config as config_mod
from . import hook as hook_mod
from . import index as index_mod
from . import promote as promote_mod

HERMES_PROFILES = Path.home() / ".hermes" / "profiles"
DEFAULT_VAULT = Path.home() / "sigil-vault"
LOG_PATH = Path.home() / "admin" / "logs" / "sigil-capture.log"
STATE_DIR = Path.home() / ".local" / "state" / "sigil-capture"
CRON_PREFIX = "[IMPORTANT: You are running as a scheduled cron job"

# Kept as module state rather than arguments so a fixture can point the whole runner at a throwaway
# profile tree and vault without a subprocess; `--vault`/`--profiles-dir` set them from the CLI.
VAULT = DEFAULT_VAULT
PROFILES = HERMES_PROFILES
LOG = LOG_PATH
DONE = STATE_DIR / "done.txt"
REPASS = STATE_DIR / "repass.json"


def log(line: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{stamp} {line}\n")


def ledger() -> dict[str, int | None]:
    """Session id -> bytes of prose that were read, or None when the line predates the field.

    An entry carrying the session id is the strong signal, but a session can legitimately yield ZERO
    entries — a cron run, a dead-end investigation — and then nothing records that it was read. Measured
    cost of not having this: the same three 2 KB sessions consumed all three slots on three consecutive
    runs while the 818 KB session never got a turn. A pass that FAILED is not recorded, so failures
    stay candidates.

    The prose size is what makes a re-pass possible: without it there is nothing to compare a grown
    session against, and every line written before this field existed therefore stays closed (#126).
    """
    out: dict[str, int | None] = {}
    try:
        for line in DONE.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if not parts:
                continue
            prose = None
            for token in parts[1:]:
                if token.startswith("prose=") and token[6:].isdigit():
                    prose = int(token[6:])
            out[parts[0]] = prose  # the last line for an id wins: the file only ever grows
    except OSError:
        pass
    return out


def mark_done(session_id: str, profile: str, entries: int, prose: int) -> None:
    DONE.parent.mkdir(parents=True, exist_ok=True)
    with DONE.open("a", encoding="utf-8") as fh:
        fh.write(f"{session_id} {datetime.now():%Y-%m-%dT%H:%M:%S} {profile} "
                 f"entries={entries} prose={prose}\n")


def _repasses() -> dict[str, list[float]]:
    try:
        raw = json.loads(REPASS.read_text(encoding="utf-8"))
        return {k: [float(x) for x in v] for k, v in raw.items()}
    except (OSError, ValueError, TypeError):
        return {}


def can_repass(session_id: str, per_day: int) -> bool:
    """Has this session been re-read too often today? A chatty session must not own the timer."""
    recent = [t for t in _repasses().get(session_id, []) if time.time() - t < 86400]
    return len(recent) < per_day


def note_repass(session_id: str) -> None:
    seen = _repasses()
    seen[session_id] = ([t for t in seen.get(session_id, []) if time.time() - t < 86400]
                        + [time.time()])
    REPASS.parent.mkdir(parents=True, exist_ok=True)
    tmp = REPASS.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen), encoding="utf-8")
    tmp.replace(REPASS)


def is_cron_session(c: sqlite3.Connection, session_id: str) -> bool:
    """A scheduled job's run is operational chatter, not memory: 58 of them sat in the queue at an
    average of 5 KB, each one cheaper than any real session, so they owned the head of the queue."""
    row = c.execute(
        """SELECT substr(content, 1, 60) FROM messages
            WHERE session_id = ? AND role = 'user' ORDER BY rowid LIMIT 1""",
        (session_id,)).fetchone()
    return bool(row and (row[0] or "").startswith(CRON_PREFIX))


def reflected_session_ids() -> set[str]:
    """Session ids some entry already carries — the strong signal that a pass produced something."""
    db = config_mod.meta_dir_for(VAULT) / "index.db"
    if not db.exists():
        return set()
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {r[0] for r in c.execute("SELECT DISTINCT session FROM entries WHERE session IS NOT NULL "
                                        "AND session != ''")}
    finally:
        c.close()


def candidates(min_age_min: float, max_age_days: float, min_prose: int,
               max_prose: int = 0, include_cron: bool = False,
               regrow: float = 1.25, regrow_per_day: int = 2) -> list[dict]:
    now = time.time()
    out: list[dict] = []
    read = ledger()
    in_vault = reflected_session_ids()
    for profile in sorted(PROFILES.iterdir()):
        db = profile / "state.db"
        if not db.is_file():
            continue
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = c.execute(
                """SELECT s.id, coalesce(s.last_activity_at, s.started_at) AS t, s.title,
                          (SELECT sum(length(m.content)) FROM messages m
                            WHERE m.session_id = s.id AND m.role IN ('user','assistant')) AS prose
                     FROM sessions s
                    WHERE coalesce(s.last_activity_at, s.started_at) BETWEEN ? AND ?
                    ORDER BY t DESC""",
                (now - max_age_days * 86400, now - min_age_min * 60)).fetchall()
        except sqlite3.Error as exc:
            log(f"error: cannot read {db}: {exc}")
            continue
        for sid, ts, title, prose in rows:
            prose = prose or 0
            if prose < min_prose or (max_prose and prose > max_prose):
                continue
            was = None
            if sid in read or sid in in_vault:
                # Already read once. Let it back in only if it has GROWN since, and only a bounded
                # number of times a day. A line without a prose size cannot prove growth, so it stays
                # closed — no stampede over the sessions captured before this rule existed.
                was = read.get(sid)
                if not was or prose < was * regrow or not can_repass(sid, regrow_per_day):
                    continue
            if not include_cron and is_cron_session(c, sid):
                continue
            out.append({"profile": profile.name, "session": sid, "prose": prose,
                        "last": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"),
                        "title": (title or "")[:48], "db": str(db),
                        "regrew_from": was})
        c.close()
    # cheapest first: the timer runs a small batch per tick, and recency order puts an 818 KB session
    # ahead of a 13 KB one, so a single monster starves everything behind it for an hour.
    out.sort(key=lambda item: item["prose"])
    return out


def heal_index() -> str:
    """Rebuild the index if a stale client left it disagreeing with the markdown (#128).

    Measured: a process holding pre-#109 code rebuilt the index and blanked `evidence_kind` and `model`
    on all 2064 rows while the markdown kept both. The audit sees it (that check ships with #128) and
    `reindex` is the repair, so this runs both on every tick — fifteen minutes of exposure instead of
    however long it takes a human to notice. Cheap: it reads the markdown and writes nothing unless the
    audit actually reports a loss.
    """
    if not VAULT.exists():
        return ""
    try:
        from .cli import audit_report
        report = audit_report(config_mod.load(VAULT))
    except Exception as exc:  # an unreadable vault must not take the timer down with it
        return f"audit unavailable: {type(exc).__name__}: {exc}"
    lost = [p for p in report.get("problems", []) if "index lost" in p]
    if not lost:
        return ""
    cfg = config_mod.load(VAULT)
    n = index_mod.reindex(cfg.root, cfg.db_path)
    return f"index self-healed :: {lost[0][:90]} -> indexed {n} entries"


def endpoint_up() -> tuple[bool, str]:
    """Is the configured chat endpoint answering? Reads the vault's own [llm] base_url."""
    url = "http://127.0.0.1:8080/v1"
    try:
        text = (config_mod.meta_dir_for(VAULT) / "config.toml").read_text(encoding="utf-8")
        block = text.split("[llm]", 1)[1].split("[", 1)[0]
        found = re.search(r'base_url\s*=\s*"([^"]+)"', block)
        if found:
            url = found.group(1)
    except (OSError, IndexError):
        pass
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/models", timeout=4):
            return True, url
    except Exception:
        return False, url


def _entries_from(said: str) -> int:
    """The hook reports `N entries`; a bare first number is the same count in its older wording."""
    found = re.search(r"(\d+)\s+entr", said)
    if found:
        return int(found.group(1))
    for token in said.replace(",", " ").split():
        if token.isdigit():
            return int(token)
    return 0


def reflect(item: dict, max_chars: int, timeout: float) -> tuple[int, str]:
    """Run one pass, with a whole-pass budget that scales with the work in it.

    `endpoint_timeout` bounds ONE request; `timeout` bounds the whole pass, and it defaults to 300 s.
    A 24-chunk session needs ~24 x 40 s, so the default guaranteed a failure that read like a model
    problem — measured: local-c1a93dec (348 KB, 24 chunks) failed at 300 s three times in a row.
    Chunks are estimated from the prose size, at 60 s each plus a margin.
    """
    chunks = max(1, -(-item["prose"] // max(1, max_chars)))
    pass_budget = max(timeout, chunks * 60 + 120)
    cfg = config_mod.load(VAULT)
    try:
        source = promote_mod.read_session_db(item["db"], item["session"])
    except ValueError as exc:
        return 1, f"cannot read {item['db']}: {exc}"
    provider = promote_mod.HttpProvider(cfg.llm_base_url, cfg.llm_model, cfg.llm_api_key,
                                        timeout=int(timeout))
    try:
        result = hook_mod.run(cfg, source, provider, agent=item["profile"],
                              session=item["session"], timeout=pass_budget, max_chars=max_chars)
    except hook_mod.HookError as exc:
        return 1, str(exc)[:300]
    except Exception as exc:  # a timer must never die with a traceback and no exit code
        return 1, f"{type(exc).__name__}: {exc}"[:300]
    return 0, result.line()[:300]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sigil-capture", description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--limit", type=int, default=0, help="max sessions per run (0 = budget-bound)")
    ap.add_argument("--budget-min", type=float, default=0,
                    help="keep going until this many minutes elapse (0 = one batch of --limit)")
    ap.add_argument("--include-cron", action="store_true",
                    help="also reflect scheduled-job sessions (off: a cron run is operational chatter)")
    ap.add_argument("--min-age", type=float, default=15, help="idle minutes (default 15)")
    ap.add_argument("--max-age-days", type=float, default=14)
    ap.add_argument("--min-prose", type=int, default=2000,
                    help="bytes of user+assistant text a session needs (default 2000)")
    ap.add_argument("--max-prose", type=int, default=0,
                    help="skip sessions above this many bytes (0 = no ceiling); a bounded backfill")
    ap.add_argument("--max-chars", type=int, default=12000,
                    help="chunk size for the pass (default 12000)")
    ap.add_argument("--timeout", type=float, default=600, help="per-request budget, seconds")
    ap.add_argument("--regrow", type=float, default=1.25,
                    help="re-read a captured session once it has grown by this factor (default 1.25)")
    ap.add_argument("--regrow-per-day", type=int, default=2,
                    help="re-passes allowed per session per day (default 2)")
    ap.add_argument("--vault", default="", help=f"vault root (default {DEFAULT_VAULT})")
    ap.add_argument("--profiles-dir", default="", help=f"profile tree (default {HERMES_PROFILES})")
    ap.add_argument("--list", action="store_true", help="print candidates, run nothing")
    ap.add_argument("--all", action="store_true", help="ignore --limit (a deliberate backfill)")
    args = ap.parse_args(argv)

    global VAULT, PROFILES, DONE, REPASS
    if args.vault:
        VAULT = Path(args.vault).expanduser()
    if args.profiles_dir:
        PROFILES = Path(args.profiles_dir).expanduser()
    if args.vault or args.profiles_dir:
        DONE = config_mod.meta_dir_for(VAULT) / "capture-done.txt"
        REPASS = config_mod.meta_dir_for(VAULT) / "capture-repass.json"

    # before anything else: the derived copy must agree with the markdown, or a session pass writes on
    # top of an index that already lost fields (the audit's own check, run on a timer — #128)
    if not config_mod.looks_like_vault(VAULT):
        # A timer pointed at a path that is not a vault must not create one and report "nothing to do"
        # forever (#141). Fail the unit so `systemctl status` says so.
        why = f"not a vault: {VAULT} — check --vault / SIGIL_VAULT. Nothing was created."
        log(why)
        print(why)
        return 1
    healed = heal_index()
    if healed:
        log(healed)
        print(healed)

    up, url = endpoint_up()
    if not up:
        log(f"endpoint unavailable at {url} — nothing attempted")
        print(f"endpoint unavailable at {url} — nothing attempted")
        return 0
    todo = candidates(args.min_age, args.max_age_days, args.min_prose, args.max_prose,
                      include_cron=args.include_cron, regrow=args.regrow,
                      regrow_per_day=args.regrow_per_day)
    if not todo:
        log("nothing to do")
        print("nothing to do")
        return 0
    if args.list:
        for item in todo:
            grew = (f"  (re-read, was {item['regrew_from'] / 1024:.0f}KB)"
                    if item.get("regrew_from") else "")
            print(f"{item['profile']:7} {item['session'][:22]:22} {item['last']} "
                  f"prose={item['prose'] / 1024:6.0f}KB  {item['title']}{grew}")
        print(f"\n{len(todo)} candidates")
        return 0

    # ONE run at a time: a timer tick and a manual backfill share a single-slot endpoint, and two
    # concurrent passes queue until one times out, which reads as a model failure. `--list` above is
    # exempt: it writes nothing. The lock is released by the kernel when the process exits.
    lock_path = STATE_DIR / "lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another sigil-capture run holds the lock — nothing attempted")
        return 0

    # A single 3-session batch per timer tick worked out at ~28 s of work per 20 minutes of wall clock,
    # so the queue never drained and the GPU looked idle. The timer now hands over a time budget and
    # this loop spends it, stopping early when there is nothing left to read.
    budget_end = time.monotonic() + args.budget_min * 60 if args.budget_min else None
    failures = done = 0
    did: set[str] = set()
    queue = list(todo)
    while queue:
        if args.limit and done >= args.limit:
            break
        if budget_end and time.monotonic() >= budget_end:
            break
        item = queue.pop(0)
        started = time.monotonic()
        rc, said = reflect(item, args.max_chars, args.timeout)
        took = time.monotonic() - started
        if rc != 0:
            failures += 1
            log(f"FAILED {item['profile']}/{item['session'][:16]} rc={rc} {took:.0f}s :: {said}")
            print(f"FAILED {item['profile']}/{item['session'][:16]} :: {said}", file=sys.stderr)
            continue
        done += 1
        did.add(item["session"])
        if item.get("regrew_from"):
            note_repass(item["session"])
        mark_done(item["session"], item["profile"], _entries_from(said), item["prose"])
        grew = (f" (re-read, grew {item['regrew_from'] / 1024:.0f}KB -> {item['prose'] / 1024:.0f}KB)"
                if item.get("regrew_from") else "")
        log(f"ok {item['profile']}/{item['session'][:16]} {took:.0f}s{grew} :: {said}")
        print(f"ok {item['profile']}/{item['session'][:16]} {took:.0f}s :: {said}")
        if not args.all:
            fresh = candidates(args.min_age, args.max_age_days, args.min_prose, args.max_prose,
                               include_cron=args.include_cron, regrow=args.regrow,
                               regrow_per_day=args.regrow_per_day)
            queue = [c for c in fresh if c["session"] not in {i["session"] for i in queue} | did]
    log(f"run finished: {done} ok, {failures} failed, budget {args.budget_min or 0:g} min")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
