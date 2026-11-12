"""Sweep query-shaping and focus-scoring variants (#48): does a blob stop winning?

The symptom: an entry that mentions every topic outranks a focused note for any single one of them.
Two candidate causes, measured separately because they get conflated:

  stopwords   the AND joins function words too ("which", "is", "the", "for", "this"), so a 4,000-word
              session report matches "everything" while a focused note that never wrote those words
              does not match at all.
  focus       bm25 rewards coverage, not concentration: hits spread through a huge body score like hits
              in a summary, so the ranking has no notion of "this entry is about that".

Every variant rides on the SHIPPED `sigil.index.search` — the stopword variant rewrites the query text
(the module tokenises it the same way), the focus variant re-scores the shipped ranking's own candidate
window. Nothing here reimplements retrieval, so "shipped" in the table is the code an agent runs.

    .venv/bin/python -m benchmarks.sweep_focus /tmp/livevault
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.vault import require_index  # noqa: E402
from sigil import index as index_mod  # noqa: E402

#: English function words plus the interrogatives a question-shaped query carries. A closed list on
#: purpose: a data-driven stoplist is a second corpus to maintain, and this one is auditable.
STOPWORDS = frozenset("""
a an and are as at be been being but by can could did do does for from had has have how i if in into
is it its just me my no not of on or our should so than that the their them then there these they
this those to too us was we were what when where which who why will with would you your
""".split())


def content_tokens(q: str) -> list[str]:
    """Query tokens with function words removed. A query of nothing but stopwords keeps them."""
    toks = index_mod._tokens(q)
    kept = [t for t in toks if t.lower() not in STOPWORDS]
    return kept or toks


def present(text: str, token: str) -> bool:
    return re.search(rf"\b{re.escape(token)}", text or "", re.I) is not None


def focus_factor(row: dict, tokens: list[str]) -> float:
    """1.0 when every query term is in the entry's handle (summary + ask), 0.5 when all of them are
    buried in body/evidence. Two numbers a human would agree with, no corpus-wide statistics."""
    handle = f"{row.get('summary') or ''} {row.get('ask') or ''}"
    whole = f"{handle} {row.get('evidence') or ''} {row.get('body') or ''}"
    in_handle = sum(1 for t in tokens if present(handle, t))
    in_whole = sum(1 for t in tokens if present(whole, t)) or 1
    return 0.5 + 0.5 * (in_handle / in_whole)


def make_search(strip_stopwords: bool, focus: bool):
    def search(db, query, kind=None, state=None, agent=None, project=None, limit=10):
        tokens = content_tokens(query)
        text = " ".join(tokens) if strip_stopwords else query
        if not focus:
            return index_mod.search(db, text, kind=kind, state=state, agent=agent,
                                    project=project, limit=limit)
        window = max(limit * 4, limit)
        rows = index_mod.search(db, text, kind=kind, state=state, agent=agent, project=project,
                                limit=window)
        for row in rows:
            row["score"] *= focus_factor(row, tokens)
        rows.sort(key=lambda r: r["score"])
        return rows[:limit]
    return search


VARIANTS = {
    "shipped": dict(strip_stopwords=False, focus=False),
    "stopwords": dict(strip_stopwords=True, focus=False),
    "focus-only": dict(strip_stopwords=False, focus=True),
    "stopwords+focus": dict(strip_stopwords=True, focus=True),
}


def score_live(db, make, queries):
    ok = promoted = 0
    misses = []
    for question, stems, _gold in queries:
        hits = make(str(db), question, limit=3)
        top = hits[0] if hits else None
        text = ((top["summary"] if top else "") or "").lower()
        if top and any(s in text for s in stems):
            ok += 1
        else:
            misses.append((question, (top["summary"][:70] if top else "<none>")))
        if top and top["state"] == "promoted":
            promoted += 1
    return ok, promoted, misses


def score_synthetic(make):
    import benchmarks.run as bench
    from benchmarks.loaders import load_synthetic
    bench.search = make
    result, _ = bench.run(load_synthetic(), [5, 10])
    m = result["metrics"]
    return m["recall_at_5"], m["recall_at_10"], m["mrr"]


def main():
    from benchmarks.live_queries import QUERIES
    arg = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    db = require_index(arg) if arg else None
    print(f"{'variant':<18}{'live ok':>8}{'live prom':>10}{'R@5':>8}{'R@10':>8}{'MRR':>8}")
    for name, kw in VARIANTS.items():
        make = make_search(**kw)
        r5, r10, mrr = score_synthetic(make)
        if db:
            ok, prom, misses = score_live(db, make, QUERIES)
            shown = f"{ok}/10", f"{prom}/10"
        else:
            misses, shown = [], ("-", "-")
        print(f"{name:<18}{shown[0]:>8}{shown[1]:>10}{r5:>8.3f}{r10:>8.3f}{mrr:>8.3f}")
        for question, top in misses:
            print(f"    MISS {question!r} -> {top}")


if __name__ == "__main__":
    main()
