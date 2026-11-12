"""Vault configuration: paths, predicates, promotion gate."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10: keep the stdlib-only promise, parse the subset we write
    tomllib = None

from . import DEFAULT_PREDICATES

#: Where derived state lives inside a vault. `.quipu` is what a pre-rename vault already has on disk;
#: it is read in place and never rewritten, so upgrading a package does not move a user's data.
META_DIR = ".sigil"
LEGACY_META_DIR = ".quipu"

#: The config file's top-level table name. A pre-rename vault heads it `[quipu]`; both parse, and new
#: vaults are written as `[sigil]`.
SECTION = "sigil"

#: The promotion gate: an entry is promoted only when its importance clears this AND it carries
#: evidence. The importer applies the same gate, so an imported vault tells the truth (#26).
DEFAULT_PROMOTE_THRESHOLD = 0.5

#: The endpoint read timeout, in seconds, when the config says nothing about it.
#:
#: One request is one chunk, and a chunk of a real session is not small: a 12-line slice of a Hermes JSONL
#: transcription is 68 KB and a 185-line session is 509 KB. At 120 s the on-box 35B tier answered a 178 KB
#: chunk with `TimeoutError: timed out` — on exactly the sessions the pass exists for, which reads as the
#: pass being broken rather than the budget being wrong. A short `transcript.md` hides this completely.
#: 600 s is chosen to survive a real chunk on a slow local tier while still failing a hung endpoint inside
#: a coffee break; a cloud tier answers in seconds and never approaches it.
DEFAULT_LLM_TIMEOUT = 600


@dataclass
class Config:
    root: Path
    predicates: tuple[str, ...] = DEFAULT_PREDICATES
    promote_threshold: float = DEFAULT_PROMOTE_THRESHOLD
    max_promoted_tokens: int = 4000
    llm_base_url: str = "http://127.0.0.1:8080/v1"
    llm_model: str = "local"
    llm_api_key: str = ""
    #: How long a single endpoint request may take before it is called a failure (#68). 120 s is
    #: smaller than one chunk of a real transcript on a slow local model, so it has to be raisable.
    llm_timeout: int = DEFAULT_LLM_TIMEOUT
    #: Optional semantic retrieval (#5). Empty `embed_url` means lexical-only, which is the default:
    #: a vault works with no endpoint, and `search --hybrid` says so instead of failing.
    embed_url: str = ""
    embed_model: str = "nomic-embed"
    extras: dict = field(default_factory=dict)

    @property
    def notes_dir(self) -> Path:
        return self.root / "notes"

    @property
    def entities_dir(self) -> Path:
        return self.root / "entities"

    @property
    def meta_dir(self) -> Path:
        """The derived-state directory: `.sigil`, or `.quipu` for a vault written before the rename.

        The name moved with the project, but a vault on disk does not get renamed by upgrading a
        package — so the old directory is read where it is. `create`-ing a new vault makes `.sigil`;
        nothing rewrites an existing `.quipu`, because the markdown is the truth and a rename must not
        move a user's data as a side effect of `sigil audit`.
        """
        return meta_dir_for(self.root)

    @property
    def db_path(self) -> Path:
        return self.meta_dir / "index.db"

    @property
    def config_path(self) -> Path:
        return self.meta_dir / "config.toml"

    def ensure(self) -> Config:
        for d in (self.root, self.notes_dir, self.entities_dir, self.meta_dir):
            d.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            self.save()
        return self

    def save(self) -> None:
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        preds = ",\n  ".join(f'"{p}"' for p in self.predicates)
        self.config_path.write_text(
            f"[{SECTION}]\n"
            f"promote_threshold = {self.promote_threshold}\n"
            f"max_promoted_tokens = {self.max_promoted_tokens}\n"
            f'predicates = [\n  {preds},\n]\n'
            "\n[llm]\n"
            f'base_url = "{self.llm_base_url}"\n'
            f'model = "{self.llm_model}"\n'
            'api_key = ""\n'
            f"timeout = {self.llm_timeout}\n"
            "\n[embed]\n"
            f'url = "{self.embed_url}"\n'
            f'model = "{self.embed_model}"\n',
            encoding="utf-8",
        )


def validate_llm_timeout(value) -> int:
    """The endpoint read timeout in seconds: a positive whole number, or a loud refusal.

    The message names `llm.timeout` because that is the key the same value lives under in
    `config.toml` — one setting, one name, whatever route it came in by.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"llm.timeout must be a whole number of seconds, got {value!r}")
    if value <= 0:
        raise ValueError(f"llm.timeout must be greater than zero, got {value!r}")
    return value


def _mini_toml(text: str) -> dict:
    """Parse the TOML subset `Config.save()` writes: [sections], `key = value`, string arrays.

    Only used on Python 3.10, where tomllib does not exist. Anything richer is refused loudly rather
    than half-understood — a config the parser fudges is worse than one it rejects.
    """
    import json
    import re

    out: dict = {}
    section: dict = out
    pending: list[str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if pending is not None:
            pending.append(line)
            if "]" in line:
                # the array is closed, so its comments can go: the comments inside it and the one
                # after the closing `]` are not part of the value
                body = " ".join(_strip_inline_comment(part) for part in pending)
                pending = None
                key, _, value = body.partition("=")
                # TOML allows a trailing comma before `]`; JSON does not
                cleaned = re.sub(r",\s*\]", "]", value.strip()).replace("'", '"')
                section[key.strip()] = json.loads(cleaned)
            continue
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = out.setdefault(line[1:-1].strip(), {})
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value.startswith("["):
            # an array is not a scalar: it may close on this line or span several, and the
            # comment after the closing `]` is not part of the value. So it is only stripped
            # once the array is closed.
            closed = _strip_inline_comment(value)
            if not closed.endswith("]"):
                pending = [line]          # multi-line array, as save() emits predicates
                continue
            section[key] = json.loads(closed.replace("'", '"'))
            continue
        value = _strip_inline_comment(value)   # a trailing `# ...` is a comment, not the value
        if value.startswith(('"', "'")):
            section[key] = value[1:-1]
        elif value in ("true", "false"):
            section[key] = value == "true"
        else:
            for cast in (int, float):   # 0.5 must parse as a float, not the string "0.5"
                try:
                    section[key] = cast(value)
                    break
                except ValueError:
                    continue
            else:
                section[key] = value
    if pending:
        raise ValueError("unterminated array in config")
    return out


def _raw_llm_timeout(text: str) -> str | None:
    """The raw `timeout = ...` value inside the `[llm]` section, if the file has one."""
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section != "llm":
            continue
        key, _, value = line.partition("=")
        if key.strip() == "timeout":
            return _strip_inline_comment(value.strip())
    return None


def _strip_inline_comment(value: str) -> str:
    """Drop a trailing `# ...` comment, unless the `#` sits inside a quoted string."""
    quote = ""
    for i, ch in enumerate(value):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return value[:i].strip()
    return value


def _refuse_bad_llm_timeout(raw: str) -> None:
    """Refuse a `timeout` that is not a whole number of seconds — in our words.

    The TOML parser would say "invalid value at line 2" and leave the operator guessing which key
    is wrong. The key is named here, before the file is parsed.
    """
    value = raw.strip()
    try:
        seconds = int(value)
    except ValueError:
        raise ValueError(f"llm.timeout must be a whole number of seconds, got {value!r}") from None
    if seconds <= 0:
        raise ValueError(f"llm.timeout must be greater than zero, got {seconds!r}")


def meta_dir_for(root: str | Path) -> Path:
    """`<root>/.sigil`, or `<root>/.quipu` for a vault written before the rename (exists wins over name)."""
    root = Path(root)
    new = root / META_DIR
    return new if (new.exists() or not (root / LEGACY_META_DIR).exists()) else root / LEGACY_META_DIR


class VaultNotFound(RuntimeError):
    """A path that is not a vault was opened for reading: say so, create nothing.

    Opening a path that is not a vault used to create one there and answer from it, so a wrong
    `--vault`, a moved checkout or a half-migrated host produced `[]` — indistinguishable from "this
    vault has no memory about that", the worst shape a memory tool can fail in (#141). Every caller
    that opens a vault for reading now has to say what happened instead. `init` and `create=True`
    still create, because that is their job.
    """


def looks_like_vault(root: str | Path) -> bool:
    """Does this directory carry a vault's shape — notes, entities, or derived-state meta?

    Deliberately generous about which of the three exists: a vault whose index was deleted, or one
    written by hand before `.quipu` existed, is still somebody's vault. The failure this guards
    against is a path with *none* of them, which is a path nobody ever initialised.
    """
    root = Path(root)
    if not root.is_dir():
        return False
    return any((root / d).is_dir() for d in ("notes", "entities", META_DIR, LEGACY_META_DIR))


def _not_a_vault(root: Path) -> VaultNotFound:
    """The refusal, in the words the caller needs: the path, what is missing, and both ways out."""
    hint = f"check --vault (or SIGIL_VAULT), or create one with `sigil init --vault {root}`"
    if not root.exists():
        return VaultNotFound(f"no such vault: {root} does not exist — {hint}. Nothing was created.")
    if not root.is_dir():
        return VaultNotFound(f"not a vault: {root} is a file, not a directory — {hint}.")
    return VaultNotFound(
        f"not a vault: {root} exists but holds no notes/, entities/ or .sigil/ — {hint}. "
        f"Nothing was created.")


def load_lenient(root: str | Path) -> Config:
    """Config for a caller that must read but must not create — a dry run.

    `sigil import --dry-run` against a vault that does not exist yet is a legitimate question ("what
    would this write?"), so it may look at a path that is not a vault: it gets that vault's own
    settings when there is a vault there, and defaults when there is not. It writes nothing either way.
    """
    root = Path(root).expanduser().resolve()
    return load(root) if looks_like_vault(root) else Config(root=root)


def load(root: str | Path, create: bool = False) -> Config:
    root = Path(root).expanduser().resolve()
    if not create and not looks_like_vault(root):
        raise _not_a_vault(root)
    cfg = Config(root=root)
    path = cfg.config_path
    if path.exists():
        text = path.read_text(encoding="utf-8")
        raw_timeout = _raw_llm_timeout(text)
        if raw_timeout is not None:
            _refuse_bad_llm_timeout(raw_timeout)
        data = tomllib.loads(text) if tomllib else _mini_toml(text)
        # a config written before the rename heads its section `[quipu]`; both must parse
        q = data.get("sigil", data.get("quipu", {}))
        cfg.predicates = tuple(q.get("predicates", DEFAULT_PREDICATES))
        cfg.promote_threshold = float(q.get("promote_threshold", cfg.promote_threshold))
        cfg.max_promoted_tokens = int(q.get("max_promoted_tokens", cfg.max_promoted_tokens))
        llm = data.get("llm", {})
        cfg.llm_base_url = llm.get("base_url", cfg.llm_base_url)
        cfg.llm_model = llm.get("model", cfg.llm_model)
        cfg.llm_api_key = llm.get("api_key", cfg.llm_api_key)
        # a config with no `timeout` key keeps the default; a config with a bad one is refused
        cfg.llm_timeout = validate_llm_timeout(llm.get("timeout", cfg.llm_timeout))
        embed = data.get("embed", {})
        cfg.embed_url = embed.get("url", cfg.embed_url) or ""
        cfg.embed_model = embed.get("model", cfg.embed_model)
        cfg.extras = data
    return cfg.ensure() if create else cfg
