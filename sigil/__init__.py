"""sigil — local-first agent memory.

Verbatim capture, a temporal knowledge graph, and lifecycle-labeled training data.
The vault is plain markdown (Obsidian-compatible); the index and graph live in SQLite.
"""

__version__ = "0.10.0"

KINDS = ("capture", "conclusion", "decision", "lesson", "fact", "handoff", "question")
STATES = ("raw", "promoted", "pruned")

DEFAULT_PREDICATES = (
    "causes", "depends_on", "implements", "supersedes", "decided_against",
    "measured_as", "located_in", "used_by", "blocks", "owned_by", "part_of",
    "contradicts", "learned_from",
)


# Hermes loads a memory provider as a DIRECTORY before it consults `hermes_agent.memory_providers`
# entry points, so the package itself is installable as the plugin dir (`.hermes/plugins/sigil ->
# this directory`). Directory loading calls `register(ctx)` here, or instantiates a top-level
# `MemoryProvider` subclass — so the class has to be exportable from the package root.
from .hermes_provider import SigilMemoryProvider  # noqa: E402,F401


def register(ctx):
    """Hermes memory-provider entry point.

    Deliberately a re-export rather than the implementation: Hermes resolves a provider's package
    directory from the entry point WITHOUT importing it, and only a package ``__init__`` yields
    one. Pointing the entry point at ``sigil.hermes_provider`` (a bare module) made a correctly
    installed provider read as "configured but not installed".
    """
    from .hermes_provider import register as _register

    return _register(ctx)
