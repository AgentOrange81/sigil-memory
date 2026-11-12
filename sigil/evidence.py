"""What kind of evidence an entry carries (#109).

The promotion gate asks one question — is there evidence at all — and a 9-byte "see log" answers it as
well as a line range does. Measured on this vault: promoted reflected entries run to a median of 151
bytes, 14% under 60, floor 9, while hand-written ones median 176. The tail is a model-quality signal,
not a claim that the memory is false, so it is recorded rather than punished: `evidence_kind` says what
the evidence IS, the gate still only asks whether it exists, and a consumer (the SFT export) can filter
on the property without the write path having to decide what is "good enough".

Four kinds, and the classification is deterministic — no model, no heuristics that drift:

- `command` — something a person could re-run: a backticked span, a `$ ` prompt, or a line that starts
  with a known binary;
- `path` — a file (optionally with a line range) that a person could open;
- `url` — something a person could fetch;
- `claim` — prose that asserts without pointing at anything re-checkable.

Everything else is `claim`, which is the honest answer for "it worked, trust me".
"""
from __future__ import annotations

import re

KINDS = ("command", "path", "url", "claim")

#: first words that make a span a command rather than an identifier
COMMANDS = frozenset({
    "git", "gh", "pytest", "python", "python3", "uv", "uvx", "pip", "sigil", "quipu", "curl", "ps", "pgrep",
    "systemctl", "journalctl", "docker", "docker-compose", "nvidia-smi", "ls", "cat", "grep", "rg",
    "sed", "awk", "tar", "sqlite3", "ssh", "scp", "rsync", "mkdir", "rm", "chmod", "sudo", "uvicorn",
    "make", "npm", "node", "cargo", "go", "rustc", "ffmpeg", "ollama", "llama-server", "df", "du",
    "free", "top", "htop", "lsof", "ss", "netstat", "ip", "ping", "tmux", "kill", "zfs", "lsblk",
    "tail", "head", "wc", "find", "stat", "date", "tree", "env", "export", "source", "open", "code",
})

_URL = re.compile(r"https?://\S+")
# a path: a slash plus an extension (optionally :line or :start-end), or an extension plus :line
_PATH = re.compile(r"(?:^|[\s(])(~?/|\.{0,2}/)?[\w.@+-]+(?:/[\w.@+-]+)*\.[A-Za-z0-9]{1,8}(?::\d+(?:-\d+)?)?")
_LINE_RANGE = re.compile(r"[\w./@+-]+\.[A-Za-z0-9]{1,8}:\d+(?:-\d+)?")
_FILE_EXT = (
    r"(?:py|md|json|jsonl|toml|ya?ml|log|db|sqlite|sh|txt|csv|html|png|jpe?g|mp4|gguf|safetensors"
    r"|service|timer|conf|cfg|ini|rs|go|c|cc|cpp|h|hpp|js|ts|tsx|ipynb)"
)
_FILE = re.compile(r"[\w./@+-]+\." + _FILE_EXT + r"(?::\d+(?:-\d+)?)?$")
_BACKTICK = re.compile(r"`([^`]+)`")
_PROMPT = re.compile(r"(?:^|\n)\s*\$\s+\S")


def _looks_like_command(span: str) -> bool:
    first = span.strip().split()[0].strip("$") if span.strip() else ""
    if first in COMMANDS:
        return True
    # `cmd --flag …` / `a && b` / `a | b`: shell shape without a known first word
    return bool(re.search(r"\s(-{1,2}\w|&&|\|\||\|)", span))


def classify(evidence: str) -> str:
    """The kind of evidence a string carries. Deterministic, and never raises."""
    text = (evidence or "").strip()
    if not text:
        return "claim"
    for span in _BACKTICK.findall(text):
        if _looks_like_command(span):
            return "command"
        if _FILE.fullmatch(span.strip()) or "/" in span and _PATH.fullmatch(span.strip()):
            return "path"  # a backticked filename still names a file
    if _PROMPT.search(text):
        return "command"
    first_line = text.splitlines()[0].strip()
    if _looks_like_command(first_line) and " " in first_line:
        return "command"
    if _LINE_RANGE.search(text):
        return "path"
    for token in text.split():
        # `MEDIA:/path/x.html;` and `docs/x.md` are files, not prose: peel the wrapper before judging
        token = token.strip("()[]<>,;\"'`")
        if ":" in token and not _LINE_RANGE.fullmatch(token):
            token = token.rsplit(":", 1)[-1] if _FILE.search(token.rsplit(":", 1)[-1]) else token
        if _FILE.fullmatch(token) and ("/" in token or _FILE.search(token)):
            return "path"
        if "/" in token and _PATH.fullmatch(token):
            return "path"
    if _URL.search(text):
        return "url"
    return "claim"
