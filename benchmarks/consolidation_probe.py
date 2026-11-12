"""Is there anything to consolidate in this vault? Measured before building, twice (#55, #54).

Consolidation — folding near-duplicate entries into one evidence-backed belief, hindsight-style — sounds
like the obvious next feature for a memory vault. On THIS corpus it has nothing to work on, and the
second measurement is the reason the answer is not merely "not much":

1. **Lexical:** exact duplicate summaries, then token-Jaccard clusters at several thresholds.
2. **Semantic:** pairwise cosine over the stored embeddings, split into "likely restatement" and
   "numbers differ on both sides" (a contradiction candidate).

What the second one exposed: 190 of the 206 high-similarity pairs came from a single 20-entry family of
*truncated identical prefixes* — the `'Corrected metadata for <topic> (agent: …, project_id:` class the
importer was producing before #54. So a similarity-driven consolidator would have spent its first pass
merging import damage, which is the opposite of what the corpus needed.

Run:  .venv/bin/python -m benchmarks.consolidation_probe ~/sigil-vault
"""
from __future__ import annotations

import itertools
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from benchmarks.vault import require_index
from sigil import embed as embed_mod
from sigil import index as index_mod
from sigil.store import iter_entries

STOP = set("the a an of to in on for and or is are was were it its this that with as at by from "
           "not do does did we our you your".split())
NUMBER = re.compile(r"\d+(?:\.\d+)?")


def tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9_.:/-]{2,}", (text or "").lower())
            if w not in STOP}


def _find(parent: dict[str, str], node: str) -> str:
    while parent[node] != node:
        parent[node] = parent[parent[node]]
        node = parent[node]
    return node


def clusters(promoted: list, toks: dict[str, set[str]], threshold: float,
             scoped: bool) -> list[list]:
    """Union-find over summary-token Jaccard; `scoped` keeps clusters inside one project."""
    parent = {e.id: e.id for e in promoted}
    for a, b in itertools.combinations(promoted, 2):
        if scoped and (a.project or "") != (b.project or ""):
            continue
        ta, tb = toks[a.id], toks[b.id]
        if not ta or not tb:
            continue
        if len(ta & tb) / len(ta | tb) >= threshold:
            ra, rb = _find(parent, a.id), _find(parent, b.id)
            if ra != rb:
                parent[rb] = ra
    groups: dict[str, list] = defaultdict(list)
    for e in promoted:
        groups[_find(parent, e.id)].append(e)
    return [g for g in groups.values() if len(g) > 1]


def lexical(vault: Path, promoted: list) -> None:
    by_summary: dict[str, list] = defaultdict(list)
    for e in promoted:
        by_summary[" ".join((e.summary or "").lower().split())].append(e.id)
    dupes = {k: v for k, v in by_summary.items() if k and len(v) > 1}
    print(f"exact duplicate summaries: {len(dupes)} groups, "
          f"{sum(len(v) for v in dupes.values())} entries")

    toks = {e.id: tokens((e.summary or "") + " " + (e.ask or "")) for e in promoted}
    for threshold in (0.55, 0.65, 0.75, 0.85):
        for scoped in (True, False):
            multi = clusters(promoted, toks, threshold, scoped)
            print(f"  jaccard>={threshold:<5} project-scoped={scoped!s:<5} "
                  f"clusters={len(multi):<3} covered={sum(len(g) for g in multi):<3} "
                  f"sizes={sorted((len(g) for g in multi), reverse=True)[:8]}")


def semantic(db: Path, promoted: list, cutoff: float = 0.86) -> None:
    by_id = {e.id: e for e in promoted}
    c = index_mod.connect(db)
    try:
        vectors = {k: embed_mod.unit(v) for k, v in embed_mod.load(c).items() if k in by_id}
    finally:
        c.close()
    print(f"\npromoted with vectors: {len(vectors)}/{len(promoted)}")
    if len(vectors) < 2:
        print("  no embeddings: run `sigil embed` first (see docs/USING.md)")
        return
    ids = list(vectors)
    pairs = []
    for a, b in itertools.combinations(ids, 2):
        score = sum(x * y for x, y in zip(vectors[a], vectors[b], strict=False))
        if score >= cutoff:
            pairs.append((score, a, b))
    pairs.sort(reverse=True)
    print(f"pairs at cosine>={cutoff}: {len(pairs)}")

    def differs(a, b) -> bool:
        na, nb = set(NUMBER.findall(a.summary + " " + a.evidence)), \
            set(NUMBER.findall(b.summary + " " + b.evidence))
        return bool(na and nb and na != nb and (na ^ nb))

    contra = [p for p in pairs if differs(by_id[p[1]], by_id[p[2]])]
    print(f"  both sides carry numbers that differ (contradiction candidates): {len(contra)}")
    print(f"  otherwise (restatements): {len(pairs) - len(contra)}")

    families = Counter()
    for _s, a, b in pairs:
        for eid in (a, b):
            fam = " ".join((by_id[eid].summary or "").split()[:3]).lower()
            families[fam] += 1
    print("  summary prefixes dominating the pairs (a family means the signal is boilerplate):")
    for fam, n in families.most_common(5):
        print(f"    {n:>4}  {fam!r}")


def main() -> int:
    vault = Path(sys.argv[1] if len(sys.argv) > 1 else "~/sigil-vault").expanduser()
    promoted = [e for _p, e in iter_entries(vault) if e.state == "promoted"]
    print(f"vault {vault}: {len(promoted)} promoted entries\n")
    lexical(vault, promoted)
    semantic(require_index(vault), promoted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
