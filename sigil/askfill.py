"""`sigil ask-backfill` — give entries the question they answer, so the corpus can become training data (#27).

`ask` is what turns a memory into a trainable pair: the question a searcher would have asked, paired with
the entry's summary/evidence as the answer. Without it an entry is recall only. On this vault 17 of 864
entries had one, which is why `sigil export` produced 18 SFT examples.

This is the one place sigil calls a model, and it is deliberately the narrowest possible call:

- **One question per entry.** ~40 output tokens, no chain, no tools, temperature 0.
- **Any OpenAI-compatible endpoint**, so it runs against the free on-box tier or a cloud key without the
  package gaining a dependency.
- **Every answer is validated before it is stored.** It must be a single line ending in `?`, it must not
  be a restatement of the summary, and it must be 12..200 bytes. A model that returns prose, a quote, or
  an empty string costs that entry and nothing else — the entry keeps its place, reported as skipped.
- **Dry run by default.** `--apply` writes the `ask:` line in place, like `sigil repair`; nothing is
  renamed and the body is not touched.

The judgement that cannot be automated: whether the question is the one a *human* would ask. That is why
`--sample N` prints the proposed pairs for review before a whole-corpus run.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .store import Entry, iter_entries

SYSTEM = (
    "You write the ONE question a person would ask to find the note below. "
    "Rules: one line only, ends with '?', under 25 words, use the note's own vocabulary (names, numbers, "
    "file paths, project words) so a keyword search finds it, and never answer the question. "
    "No preamble, no quotes, no 'Question:' prefix. If the note carries no claim worth asking about, "
    "reply exactly NONE."
)
#: A question shorter than this is a label ("What linking?"), not a search handle.
MIN_BYTES, MAX_BYTES = 20, 200


@dataclass
class Proposal:
    id: str
    summary: str
    ask: str = ""
    reason: str = ""
    path: Path | None = None


@dataclass
class Result:
    proposals: list[Proposal] = field(default_factory=list)
    written: int = 0
    skipped: int = 0
    failed: int = 0


def candidates(vault: str | Path, state: str = "promoted", limit: int = 0) -> list[tuple[Path, Entry]]:
    """Entries that have a summary and no `ask`, newest-importance first."""
    out = [(Path(p), e) for p, e in iter_entries(vault)
           if (e.summary or "").strip() and not (e.ask or "").strip()
           and (not state or e.state == state)]
    out.sort(key=lambda pe: (-float(pe[1].importance or 0.0), pe[1].created or "", pe[1].id))
    return out[:limit] if limit else out


def _clean(raw: str) -> str:
    text = (raw or "").strip().splitlines()[0] if (raw or "").strip() else ""
    text = re.sub(r"^(?:question|q)\s*:\s*", "", text, flags=re.I)
    text = text.strip().strip('"').strip("'").strip()
    text = re.sub(r"\s+", " ", text)
    # a question-shaped line missing only its '?' is a formatting miss, not a bad question: normalise it
    if text and not text.endswith("?") and text.lower().startswith(_INTERROGATIVE):
        text = text.rstrip(".") + "?"
    return text


_INTERROGATIVE = ("what", "which", "how", "why", "when", "where", "who", "does", "did", "is", "are",
                  "can", "should", "could", "would", "was", "were", "has", "have")


def validate(ask: str, summary: str) -> str:
    """Return an error message, or "" when the question is usable."""
    if not ask:
        return "empty"
    if ask.upper() == "NONE":
        return "model says there is no claim worth asking about"
    if not ask.endswith("?"):
        return "does not end with '?'"
    if "\n" in ask:
        return "more than one line"
    size = len(ask.encode("utf-8"))
    if size < MIN_BYTES:
        return f"too short ({size} bytes)"
    if size > MAX_BYTES:
        return f"too long ({size} bytes)"
    # a "question" that is the summary with a '?' bolted on helps nobody: it is the answer, not the ask
    norm = lambda s: re.sub(r"[^a-z0-9 ]", "", (s or "").lower())  # noqa: E731
    a, b = norm(ask), norm(summary)
    if b and (b in a or a.rstrip("? ") == b):
        return "restates the summary instead of asking"
    return ""


def ask_for(entry: Entry, url: str, model: str, api_key: str = "", timeout: float = 120.0) -> str:
    """One completion, one question. Raises on transport errors so the caller can count them."""
    prompt = f"summary: {entry.summary}\n"
    if entry.evidence:
        prompt += f"evidence: {entry.evidence[:400]}\n"
    if entry.project:
        prompt += f"project: {entry.project}\n"
    # a reasoning model spends the budget on `reasoning_content` first: too small a cap truncates the
    # answer mid-question, which then fails validation and looks like a bad model rather than a bad cap
    payload = {"model": model, "temperature": 0.0, "max_tokens": 400,
               "messages": [{"role": "system", "content": SYSTEM},
                            {"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.load(resp)
    message = body["choices"][0]["message"]
    # a reasoning model puts its thinking in `reasoning_content`; the answer is in `content`
    return _clean(message.get("content") or "")


def run(vault: str | Path, url: str, model: str, api_key: str = "", limit: int = 0,
        state: str = "promoted", progress=None) -> Result:
    result = Result()
    for index, (path, entry) in enumerate(candidates(vault, state=state, limit=limit), 1):
        prop = Proposal(id=entry.id, summary=entry.summary, path=path)
        try:
            raw = ask_for(entry, url, model, api_key)
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as exc:
            prop.reason = f"endpoint error: {type(exc).__name__}"
            result.failed += 1
            result.proposals.append(prop)
            continue
        problem = validate(raw, entry.summary)
        if problem:
            prop.reason = problem
            result.skipped += 1
        else:
            prop.ask = raw
        result.proposals.append(prop)
        if progress:
            progress(index, prop)
    return result


def apply(vault: str | Path, result: Result) -> int:
    """Write the `ask:` line in place. Same path, same body, no rename."""
    written = 0
    for prop in result.proposals:
        if not prop.ask or not prop.path:
            continue
        text = prop.path.read_text(encoding="utf-8", errors="replace")
        head, sep, body = text.partition("\n---")
        if not sep:
            prop.reason = "no frontmatter fence"
            continue
        value = json.dumps(prop.ask, ensure_ascii=False)
        if re.search(r"^ask:.*$", head, flags=re.M):
            head = re.sub(r"^ask:.*$", f"ask: {value}", head, count=1, flags=re.M)
        else:
            head = re.sub(r"^(summary:.*)$", f"\\1\nask: {value}", head, count=1, flags=re.M)
        prop.path.write_text(head + sep + body, encoding="utf-8")
        written += 1
    result.written = written
    return written


def reindex(vault: str | Path) -> None:
    from . import config as config_mod
    from . import index as index_mod

    cfg = config_mod.load(vault, create=True)
    index_mod.reindex(cfg.root, cfg.db_path)
