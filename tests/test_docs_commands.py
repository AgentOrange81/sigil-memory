"""docs/USING.md and README.md turned into executable assertions (doc-drift QA).

Scope: tests/ only — quipu/, docs/ and README.md are read, never edited. docs/USING.md:3 claims
"Every command here exists on `main` today". So the docs cannot rot silently, these tests:

1. parse every `quipu <subcommand>` invocation out of the fenced shells AND the inline code spans
   of both docs — including global-flag-first forms like `quipu --vault ~/memory init`, whose
   subcommand is not the first token after `quipu` — and require each named subcommand to exist:
   `quipu <cmd> --help` must exit 0;
2. parse every `--flag` and attribute it by POSITION: flags after the subcommand must be accepted
   by that subcommand's --help; flags before it (globals) by the root parser. Flags named in prose
   with no command beside them (attribution would be guesswork) only have to exist on SOME parser;
3. for the commands the docs spell out, the tests do not retype them: they are DERIVED from the
   doc text by strict helpers — one distinct normalised command form (the same command quoted in
   both files is one form); if a probe matches several distinct forms, it fails listing every
   form with its file, line and mention count — and then run hermetically. Only three
   substitutions are allowed: the doc's `~/...` example destination
   becomes the test vault (argument structure unchanged), genuine placeholders (<dir>,
   <edge-id>) are filled in, and values the doc quotes from its own example run (entry ids) are
   replaced by ones from the test vault. If the docs change, the derived commands change with
   them — a doc regression fails the test for the right reason;
4. hold the docs to their own existence promise: a subcommand that is NOT implemented must carry
   a "planned ... (#N)" marker in the same bullet (with its continuation lines) or the same
   sentence, and such a marker may not point at an issue PLAN.md marks done (hermetic local
   check: PLAN.md is the repo's own tracker, tests touch no network).

A failing test here means the DOC is wrong (or the CLI regressed from what the docs promise); the
finding is reported to the maintainers, the doc is not edited by QA. Known wrong claims are left
asserting the documented behaviour on purpose — they stay red until the doc or the CLI is fixed.
"""

import functools
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from sigil import config as config_mod

REPO = Path(__file__).resolve().parent.parent
DOC_FILES = (REPO / "docs" / "USING.md", REPO / "README.md")
PLAN = REPO / "PLAN.md"

QUIPU_WORD = re.compile(r"(?<![\w/.\-])(?:sigil|quipu)(?=\s)")  # docs say sigil; quipu for one release
FLAG = re.compile(r"--[a-z][a-z0-9-]*")
SPANNED = re.compile(r"`([^`]+)`")
PLANNED_MARKER = re.compile(r"planned[^.\n)]{0,120}?#(\d+)\b")


# --- parsing the docs ----------------------------------------------------------------


def _join_continuations(chunk):
    """Join fenced lines ending in `\\` into single logical lines (both write examples use them)."""
    joined = []
    for no, text in chunk:
        prev = joined[-1] if joined else None
        if prev is not None and prev[1].rstrip().endswith("\\"):
            merged = prev[1].rstrip()[:-1].rstrip() + " " + text.strip()
            joined[-1] = (prev[0], merged)
        else:
            joined.append((no, text))
    return joined


def subcommand_of(argv):
    """First positional token, skipping global-flag/value pairs: `--vault ~/memory init` -> init."""
    i = 0
    while i < len(argv):
        if argv[i].startswith("--"):
            i += 2 if i + 1 < len(argv) and not argv[i + 1].startswith("--") else 1
            continue
        return argv[i]
    return None


def _tokens_before_comment(tokens):
    for tok in tokens:
        if tok.startswith("#"):
            return
        yield tok


def _scan_command(path, no, display, src, out):
    """One doc location naming a quipu invocation: mentions, flags by position, full argv.

    `src` is the span/line text that constitutes the command itself (fragment matching uses it),
    `display` is the whole doc line (failure messages quote it).
    """
    if not QUIPU_WORD.search(src):
        # a span naming a flag with no command beside it: it must still exist on SOME parser
        for tok in FLAG.findall(src):
            out["loose_flags"].append({"file": path, "line": no, "text": display, "flag": tok})
        return
    try:
        tokens = list(_tokens_before_comment(shlex.split(src)))
    except ValueError:
        return
    if not tokens or tokens[0] not in ("sigil", "quipu") or len(tokens) < 2:
        return
    argv, cmd = tokens[1:], subcommand_of(tokens[1:])
    if cmd is None or cmd not in argv:
        return
    out["commands"].append({"file": path, "line": no, "text": display, "src": src,
                            "cmd": cmd, "argv": argv})
    out["mentions"].append({"file": path, "line": no, "text": display, "cmd": cmd})
    at = argv.index(cmd)
    for tok in argv[:at]:
        if FLAG.fullmatch(tok):
            out["global_pairs"].append({"file": path, "line": no, "text": display, "flag": tok})
    for tok in argv[at + 1:]:
        if FLAG.fullmatch(tok):
            out["strict_pairs"].append({"file": path, "line": no, "text": display,
                                        "cmd": cmd, "flag": tok})


def parse_docs():
    """Mentions, position-attributed flag pairs and full argv for every documented invocation."""
    out = {"mentions": [], "strict_pairs": [], "global_pairs": [], "loose_flags": [], "commands": []}
    for path in DOC_FILES:
        lines = path.read_text(encoding="utf-8").splitlines()
        in_fence, chunk, fenced = False, [], []
        for no, line in enumerate(lines, 1):
            if line.lstrip().startswith("```"):
                if in_fence:
                    fenced.extend(_join_continuations(chunk))
                    chunk = []
                in_fence = not in_fence
                continue
            if in_fence:
                chunk.append((no, line))
                continue
            for span in SPANNED.findall(line):
                _scan_command(path, no, line, span.strip(), out)
        if in_fence and chunk:  # unterminated fence: keep scanning its lines anyway
            fenced.extend(_join_continuations(chunk))
        for no, text in fenced:
            _scan_command(path, no, text, text, out)
    return out


def _dedupe(items, keyfields):
    seen, out = set(), []
    for item in items:
        key = tuple(str(item[f]) for f in keyfields)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


PARSED = parse_docs()
MENTIONS = _dedupe(PARSED["mentions"], ("file", "line", "cmd"))
STRICT_PAIRS = _dedupe(PARSED["strict_pairs"], ("cmd", "flag"))
GLOBAL_PAIRS = _dedupe(PARSED["global_pairs"], ("flag",))
LOOSE_FLAGS = _dedupe(PARSED["loose_flags"], ("flag",))
ALL_COMMANDS = PARSED["commands"]


# --- derivation helpers (strict: exactly one match, or fail listing the candidates) ---


def doc_command(fragments):
    """The distinct documented invocation(s) matching every fragment.

    Each candidate is normalised to its argv form — shlex collapses quotes, escapes and
    whitespace, the doc's trailing `# comment` prose is already dropped — and candidates are
    grouped by that normalised form. Two files quoting the SAME command is normal and fine: one
    distinct form is used and asserted once (N executions would only multiply runtime, and the
    identical form carries no extra information). Two or more DISTINCT forms for the same probe
    are the ambiguity that matters: fail, listing every form with its file, line and mention
    count. Never falls back to a hardcoded command; never picks the first silently.
    """
    groups: dict[tuple, list] = {}
    for cand in ALL_COMMANDS:
        if not all(f in cand["src"] for f in fragments):
            continue
        groups.setdefault(tuple(cand["argv"]), []).append(cand)
    if len(groups) == 1:
        return next(iter(groups.values()))[0]
    listed = "\n      ".join(
        f"{g[0]['file'].name}:{g[0]['line']} {g[0]['text'].strip()[:90]!r} "
        f"(argv {g[0]['argv']}, mentioned {len(g)}x)"
        for g in sorted(groups.values(), key=lambda g: (g[0]["file"].name, g[0]["line"])))
    if not groups:
        pytest.fail(f"no documented command matches {list(fragments)} "
                    f"(scanned {len(ALL_COMMANDS)} documented invocations)")
    pytest.fail(f"expected exactly one distinct documented command matching {list(fragments)}, "
                f"found {len(groups)} different forms:\n      {listed}")


def _fences():
    """Every fenced block of every doc file as (start_no, [(line_no, joined_line), ...])."""
    blocks = []
    for path in DOC_FILES:
        lines = path.read_text(encoding="utf-8").splitlines()
        in_fence, start, chunk = False, 0, []
        for no, line in enumerate(lines, 1):
            if line.lstrip().startswith("```"):
                if in_fence:
                    blocks.append((start, _join_continuations(chunk)))
                    chunk = []
                else:
                    start = no
                in_fence = not in_fence
                continue
            if in_fence:
                chunk.append((no, line))
        if in_fence and chunk:
            blocks.append((start, _join_continuations(chunk)))
    return blocks


def quickstart_init():
    """The init line of README's quickstart fence (the bash fence carrying the install line).

    Strict: the quickstart fence must contain exactly one init command.
    """
    for _start, chunk in _fences():
        block = "\n".join(text for _no, text in chunk)
        if "pip install" not in block:
            continue
        nos = {no for no, _t in chunk}
        inits = [c for c in ALL_COMMANDS if c["file"].name == "README.md" and c["line"] in nos
                 and subcommand_of(c["argv"]) == "init"]
        if len(inits) != 1:
            listed = "\n      ".join(
                f"{c['file'].name}:{c['line']} {c['text'].strip()!r}" for c in inits)
            pytest.fail(f"README's quickstart fence must contain exactly one init command, "
                        f"found {len(inits)}:\n      {listed or '(none)'}")
        return inits[0]
    pytest.fail("README has no quickstart fence (a bash fence containing 'pip install')")


def doc_quote(pattern):
    """A unique quoted fragment from the docs (quoted error strings, example values), by regex."""
    hits = []
    for path in DOC_FILES:
        hits.extend((path, m) for m in re.finditer(pattern, path.read_text(encoding="utf-8")))
    if len(hits) != 1:
        listed = "; ".join(f"{p.name}: {m.group(0)!r}" for p, m in hits)
        pytest.fail(f"expected exactly one doc fragment matching {pattern!r}, found {len(hits)}: "
                    f"{listed or '(none)'}")
    return hits[0][1]


def argv_from(entry, vault=None, mapping=None, value_after=None):
    """Doc argv -> runnable argv.

    `~/...` destinations (the doc's example $HOME) become the test vault — the argument structure
    stays exactly as documented; placeholder tokens are filled from `mapping`; `value_after`
    replaces a flag's value when the doc quotes it from its own recorded run (the --supersedes id).
    """
    argv = []
    for tok in entry["argv"]:
        repl = None
        if vault is not None and tok.startswith("~"):
            repl = str(vault)
        elif mapping and tok in mapping:
            repl = str(mapping[tok])
        elif value_after and argv and argv[-1] in value_after:
            repl = str(value_after[argv[-1]])
        argv.append(repl if repl is not None else tok)
    return argv


# --- the CLI, driven exactly as a user would drive it --------------------------------


def run_cli(*args, stdin_text=None, cwd=None):
    """Run `python -m quipu <args>` capturing exit code and output.

    stdin is /dev/null unless text is piped, so "no input" is deterministic instead of being
    inherited from the pytest session. Hermetic: every call drives a tmp vault, no network and no
    model anywhere (reflect only ever runs with --fake).
    """
    return subprocess.run(
        [sys.executable, "-m", "sigil", *args],
        capture_output=True, text=True, timeout=60, cwd=cwd,
        input=stdin_text, stdin=None if stdin_text is not None else subprocess.DEVNULL,
    )


@functools.cache
def help_exit_code(cmd):
    return run_cli(cmd, "--help").returncode


@functools.cache
def subcommand_flags(cmd):
    """The flags `--help` documents for a subcommand, parsed — never a hardcoded list.

    Subcommand-specific options may live one level deeper (import fabric); when the help text
    advertises a {choice,...} argument, those help pages are folded in as well.
    """
    out = run_cli(cmd, "--help")
    assert out.returncode == 0, f"quipu {cmd} --help must exit 0"
    flags = set(FLAG.findall(out.stdout))
    joined = out.stdout + out.stderr
    for choices in re.findall(r"\{([a-z][a-z0-9-]*(?:,[a-z][a-z0-9-]*)*)\}", joined):
        for choice in choices.split(","):
            flags |= set(FLAG.findall(run_cli(cmd, choice, "--help").stdout))
    return flags


@functools.cache
def root_parser_flags():
    """Flags accepted by the ROOT parser (globals like --vault live there, not on subparsers)."""
    out = run_cli("--help")
    assert out.returncode == 0, "quipu --help must exit 0"
    return set(FLAG.findall(out.stdout))


@functools.cache
def all_parser_flags():
    """Every flag any subcommand's or the root parser's --help advertising, for loose-flag checks."""
    flags = set(root_parser_flags())
    for cmd in sorted({m["cmd"] for m in MENTIONS if help_exit_code(m["cmd"]) == 0}):
        flags |= subcommand_flags(cmd)
    return flags


def make_vault(tmp_path):
    """A vault created by the documented quickstart init command, retargeted into tmp."""
    vault = tmp_path / "vault"
    entry = quickstart_init()
    out = run_cli(*argv_from(entry, vault=vault))
    assert out.returncode == 0, out.stderr
    return vault


# --- 1. every documented subcommand exists -------------------------------------------


@pytest.mark.parametrize("mention", MENTIONS, ids=lambda m: f"{m['file'].name}:{m['line']}:{m['cmd']}")
def test_documented_subcommand_exists_or_is_marked_planned(mention):
    """docs/USING.md:3 promises 'Every command here exists on `main` today'.

    A subcommand the parser does not know may appear in the docs only with a "planned ... (#N)"
    marker in the same bullet (the README writes it on a bullet's continuation line) or the same
    sentence — the doc must say so honestly in the reader's reading unit, not merely on the same
    physical line.
    """
    if help_exit_code(mention["cmd"]) == 0:
        return
    lines = mention["file"].read_text(encoding="utf-8").splitlines()
    scope = _marker_scope(lines, mention["line"])
    assert PLANNED_MARKER.search(scope), (
        f"{mention['file'].name}:{mention['line']} documents 'quipu {mention['cmd']}' but the "
        f"parser has no such command and no planned marker sits in the same bullet or sentence: "
        f"{mention['text'].strip()!r}"
    )


def _marker_scope(lines, no):
    """Same bullet (with its indented continuation lines) when in a list, else the same line."""
    line = lines[no - 1]
    if line.lstrip().startswith(("-", "*", "+")):
        block = [line]
        for nxt in lines[no:]:
            if not nxt.strip() or nxt.lstrip().startswith(("-", "*", "##")):
                break
            if nxt.startswith((" ", "\t")):
                block.append(nxt)
            else:
                break
        return "\n".join(block)
    return line


# --- 2. every documented flag is accepted by the parser that owns it -----------------


@pytest.mark.parametrize("pair", STRICT_PAIRS, ids=lambda p: f"{p['cmd']} {p['flag']}")
def test_flag_after_subcommand_is_accepted_by_that_subcommand(pair):
    accepted = pair["flag"] in subcommand_flags(pair["cmd"])
    assert accepted, (
        f"{pair['file'].name}:{pair['line']} documents '{pair['flag']}' after "
        f"'quipu {pair['cmd']}', but 'quipu {pair['cmd']} --help' does not list it: "
        f"{pair['text'].strip()!r}"
    )


@pytest.mark.parametrize("pair", GLOBAL_PAIRS, ids=lambda p: f"global {p['flag']}")
def test_flag_before_subcommand_is_accepted_by_the_root_parser(pair):
    """README.md:22 — '`--vault` is a global flag: it goes **before** the subcommand.'"""
    accepted = pair["flag"] in root_parser_flags()
    assert accepted, (
        f"{pair['file'].name}:{pair['line']} documents '{pair['flag']}' before the subcommand "
        f"(a global), but 'quipu --help' does not list it: {pair['text'].strip()!r}"
    )


@pytest.mark.parametrize("loose", LOOSE_FLAGS, ids=lambda f: f["flag"])
def test_flag_named_in_prose_exists_somewhere_in_the_cli(loose):
    assert loose["flag"] in all_parser_flags(), (
        f"{loose['file'].name}:{loose['line']} mentions '{loose['flag']}' but no quipu parser "
        f"accepts it: {loose['text'].strip()!r}"
    )


# --- 4. the docs' own "exists on main" promise and planned markers -------------------


def _plan_says_done(issue):
    """Hermetic 'is issue #N closed' check against the repo's own tracker (no network).

    '#N' must appear in PLAN.md inside a done milestone ('(done') or beside a checked [x] item
    for the marker to be judged stale. An issue PLAN.md does not mention cannot be judged here
    and is not failed — a local false positive matters more than a missed remote truth.
    """
    if not PLAN.exists():
        return False
    lines = PLAN.read_text(encoding="utf-8").splitlines()
    section = ""
    for no, line in enumerate(lines):
        if line.startswith("##"):
            section = line
        if re.search(rf"#{issue}\b", line):
            window = lines[max(0, no - 2):no + 3]
            if "(done" in section.lower() or any("[x]" in w for w in window):
                return True
    return False


def test_planned_markers_do_not_point_at_done_issues():
    for path in DOC_FILES:
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for issue in PLANNED_MARKER.findall(line):
                assert not _plan_says_done(issue), (
                    f"{path.name}:{no} marks a command 'planned ... (#{issue})' but PLAN.md "
                    f"records that issue as done — the marker is stale and the command should "
                    f"either work or reference an open plan"
                )


# --- 3. the documented commands, derived from the doc and run hermetically -----------


def test_readme_quickstart_init_runs(tmp_path):
    """README's quickstart init line, derived from the doc and run exactly as documented.

    The doc's example destination `~/memory` is retargeted into tmp_path, keeping the argument
    structure identical, because the tests stay hermetic. If the doc reverts to a form the CLI
    rejects (as the pre-#22 quickstart did — `quipu init --vault ...`), this fails again for the
    right reason.
    """
    entry = quickstart_init()
    vault = tmp_path / "quickstart-vault"
    out = run_cli(*argv_from(entry, vault=vault))
    assert out.returncode == 0, (
        f"README:{entry['line']} documents {entry['text'].strip()!r} but it exits "
        f"{out.returncode}: {out.stderr.strip()[:200]!r}"
    )


def test_init_creates_a_vault_with_the_documented_config(tmp_path):
    vault = make_vault(tmp_path)
    # docs/USING.md:60 — promote_threshold is 0.5 in .sigil/config.toml
    cfg = config_mod.load(vault, create=False)
    assert cfg.promote_threshold == 0.5
    assert "promote_threshold = 0.5" in (vault / ".sigil" / "config.toml").read_text(encoding="utf-8")
    # docs/USING.md:131 — the reflection endpoint default
    assert cfg.llm_base_url == "http://127.0.0.1:8080/v1"


def test_capture_takes_text_as_an_argument(tmp_path):
    """README's quickstart capture line, derived: the text and --project come from the doc."""
    vault = make_vault(tmp_path)
    out = run_cli(*argv_from(doc_command(["capture", "--project"]), vault=vault))
    assert out.returncode == 0, out.stderr
    assert re.fullmatch(r"[0-9a-f]{8}\n?", out.stdout), f"expected a bare id, got {out.stdout!r}"


def test_capture_also_takes_text_on_stdin(tmp_path):
    """docs/USING.md:42 — 'capture and write take the text as an argument or on stdin'."""
    vault = make_vault(tmp_path)
    out = run_cli(*argv_from(doc_command(["capture", "--project"]), vault=vault),
                  stdin_text="piped capture line\n")
    assert out.returncode == 0
    assert re.fullmatch(r"[0-9a-f]{8}\n?", out.stdout), f"expected a bare id, got {out.stdout!r}"


def test_search_prints_no_matches_and_exits_1(tmp_path):
    """docs/USING.md:22 — 'No match prints `no matches` and exits 1'."""
    vault = make_vault(tmp_path)
    out = run_cli("--vault", str(vault), "search", "zzz-not-in-vault")
    assert out.returncode == 1
    assert "no matches" in out.stdout


def test_search_with_filters_returns_rows(tmp_path):
    """docs/USING.md:12 — the documented search command, derived; the filters must filter."""
    vault = make_vault(tmp_path)
    seeded = run_cli(*argv_from(doc_command(["capture", "--project"]), vault=vault))
    assert seeded.returncode == 0, seeded.stderr
    other = run_cli("--vault", str(vault), "capture", "unrelated note about the other project",
                    "--project", "other")
    assert other.returncode == 0, other.stderr
    out = run_cli(*argv_from(doc_command(["search", "--limit"])), cwd=str(vault))
    assert out.returncode == 0, out.stderr
    assert "DOCP config threw 649 CCEs" in out.stdout, "the documented query must find its hit"
    assert "unrelated note" not in out.stdout, "--project must filter"


def test_documented_correction_produces_a_preference_pair(tmp_path):
    """docs/USING.md:84-107 — the full write command (derived; only the --supersedes id, which
    the doc quotes from its own example run, is replaced by one from this vault), then the
    documented export dpo command and the record shape the doc promises."""
    vault = make_vault(tmp_path)
    old = run_cli(*argv_from(doc_command(["capture", "--project"]), vault=vault))
    old_id = old.stdout.strip()
    write_entry = doc_command(["--supersedes"])
    out = run_cli(*argv_from(write_entry, value_after={"--supersedes": old_id}), cwd=str(vault))
    assert out.returncode == 0, out.stderr
    exp = run_cli(*argv_from(doc_command(["export", "dpo", "--out", "--vault"]), vault=vault),
                  cwd=str(tmp_path))
    assert exp.returncode == 0 and "1 preference pairs" in exp.stdout, exp.stderr
    record = json.loads((tmp_path / "data" / "dpo.jsonl").read_text(encoding="utf-8").splitlines()[0])
    # docs/USING.md:97-107 — prompt, both answers, and the deciding evidence as meta.reason
    assert record["prompt"] == "why did the box reboot at 03:02?"
    assert "DOCP 3600 is stable again" in record["chosen"]
    assert record["chosen"] != record["rejected"] and record["rejected"]
    assert record["meta"]["reason"] == "3 consecutive boots, 0 MCE lines each"
    assert record["meta"]["superseded"]["entry"] == old_id


def test_write_rejects_a_malformed_edge_exactly_as_documented(tmp_path):
    """docs/USING.md:155-156 — both the malformed value and the expected message are derived
    from the doc's own quoted error, then fed to the CLI."""
    vault = make_vault(tmp_path)
    bad_value = doc_quote(r"got '([^']+)'").group(1)
    expected = doc_quote(r"`(--edge wants src:predicate:dst, got '[^`]+')`").group(1)
    out = run_cli("--vault", str(vault), "write", "--summary", "probe", "--edge", bad_value)
    assert out.returncode == 2
    assert expected in out.stderr


def test_write_with_bad_predicate_is_unindexed_until_reindex(tmp_path):
    """docs/USING.md:150-161 — the quoted ValueError (derived) and the documented flow: the entry
    sits on disk unindexed until the documented reindex command runs."""
    vault = make_vault(tmp_path)
    predicate = doc_quote(r"predicate\s+'(\w+)' is not in the vocabulary").group(1)
    doc_argv = doc_command(["--supersedes"])["argv"]  # carries the doc's own SRC:PREDICATE:DST shape
    src_name = next(tok for tok in doc_argv if tok.count(":") >= 2).split(":", 1)[0]
    out = run_cli("--vault", str(vault), "write", "--summary", "probe entry for bad predicate",
                  "--edge", f"{src_name}:{predicate}:docp")
    assert out.returncode != 0
    assert f"predicate '{predicate}' is not in the vocabulary" in out.stderr
    s1 = run_cli("--vault", str(vault), "search", "probe entry for bad predicate")
    assert s1.returncode == 1 and "no matches" in s1.stdout
    rx = run_cli(*argv_from(doc_command(["reindex"])), cwd=str(vault))
    assert rx.returncode == 0 and "indexed" in rx.stdout
    s2 = run_cli("--vault", str(vault), "search", "probe entry for bad predicate")
    assert s2.returncode == 0, "after reindex the entry must be findable"


def _doc_edge_argv():
    """`quipu edge SRC PRED DST`, seeded from the doc's own --edge example value."""
    entry = doc_command(["--supersedes"])
    triple = next(tok for tok in entry["argv"] if tok.count(":") >= 2)
    parts = triple.split(":", 2)
    assert len(parts) == 3, f"the documented --edge value must be SRC:PRED:DST, got {triple!r}"
    return ["edge", *parts]


def test_timeline_shows_edges_with_validity_windows(tmp_path):
    """docs/USING.md:24-29 — `quipu timeline trx40`, derived from the doc, on a seeded edge."""
    vault = make_vault(tmp_path)
    seed = run_cli("--vault", str(vault), *_doc_edge_argv())
    assert seed.returncode == 0, seed.stderr
    out = run_cli(*argv_from(doc_command(["timeline", "trx40"])), cwd=str(vault))
    assert out.returncode == 0
    assert "-measured_as->" in out.stdout
    assert "→ now" in out.stdout, "a live edge is shown as ending 'now'"


def test_invalidate_closes_and_contradict_replaces_the_edge(tmp_path):
    """docs/USING.md:109-113 — the graph does not follow --supersedes; the documented invalidate
    and contradict commands (derived) close the window and open the replacement."""
    vault = make_vault(tmp_path)
    seed = run_cli("--vault", str(vault), *_doc_edge_argv())
    assert seed.returncode == 0, seed.stderr
    eid = run_cli(*argv_from(doc_command(["timeline", "trx40"])), cwd=str(vault)).stdout.split()[0]

    inv = run_cli(*argv_from(doc_command(["invalidate"]), mapping={"<edge-id>": eid}), cwd=str(vault))
    assert inv.returncode == 0 and f"closed edge {eid}" in inv.stdout
    closed = run_cli(*argv_from(doc_command(["timeline", "trx40"])), cwd=str(vault))
    assert "→ now" not in closed.stdout, "the closed window must no longer read 'now'"

    con = run_cli(*argv_from(doc_command(["contradict"]),
                             mapping={"<edge-id>": eid, "src": "TRX40",
                                      "pred": "measured_as", "dst": "docp-3200"}), cwd=str(vault))
    assert con.returncode == 0, con.stderr
    assert con.stdout.strip().isdigit(), "contradict prints the replacement edge id"
    final = run_cli(*argv_from(doc_command(["timeline", "trx40"])), cwd=str(vault))
    assert len(final.stdout.strip().splitlines()) == 2, "closed row kept + replacement opened"
    assert "docp-3200" in final.stdout and "→ now" in final.stdout


def test_export_graph_marks_closed_edges_live_false(tmp_path):
    """docs/USING.md:113 — the documented export graph command (derived): closed rows keep their
    window as live: false."""
    vault = make_vault(tmp_path)
    seed = run_cli("--vault", str(vault), *_doc_edge_argv())
    assert seed.returncode == 0, seed.stderr
    eid = run_cli(*argv_from(doc_command(["timeline", "trx40"])), cwd=str(vault)).stdout.split()[0]
    run_cli(*argv_from(doc_command(["invalidate"]), mapping={"<edge-id>": eid}), cwd=str(vault))
    other = run_cli("--vault", str(vault), "edge", "a", "part_of", "b")
    assert other.returncode == 0, other.stderr

    exp = run_cli(*argv_from(doc_command(["export", "graph", "--out"])), cwd=str(vault))
    assert exp.returncode == 0 and "edges" in exp.stdout, exp.stderr
    out_path = vault / "data" / "graph" / "edges.jsonl"
    records = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    assert {r["live"] for r in records} == {True, False}, \
        "closed rows keep their window as live: false"


def test_export_sft_record_shape(tmp_path):
    """docs/USING.md:119 — the documented export sft command (derived, with the documented
    --system flag): messages and full meta."""
    vault = make_vault(tmp_path)
    seeded = run_cli("--vault", str(vault), "write", "--summary", "stable after the flash",
                     "--evidence", "3 consecutive boots", "--ask", "why did the box reboot at 03:02?")
    assert seeded.returncode == 0, seeded.stderr
    out = run_cli(*argv_from(doc_command(["export", "sft", "--out", "--vault"]), vault=vault)
                  + ["--system", "be brief"], cwd=str(tmp_path))
    assert out.returncode == 0 and "1 sft examples" in out.stdout, out.stderr
    record = json.loads((tmp_path / "data" / "sft.jsonl").read_text(encoding="utf-8").splitlines()[0])
    roles = [m["role"] for m in record["messages"]]
    assert roles == ["system", "user", "assistant"]
    assert record["messages"][1]["content"] == "why did the box reboot at 03:02?"
    expected_meta = {"entry", "kind", "state", "agent", "project", "session", "created",
                     "confidence", "importance", "source", "evidence_kind", "evidence_bytes"}
    assert set(record["meta"]["sigil"]) == expected_meta


def test_stats_reports_promotion_rate_as_promoted_over_total(tmp_path):
    """docs/USING.md:135 — the documented stats command (derived): promotion_rate = promoted ÷ total."""
    vault = make_vault(tmp_path)
    for text in ("raw note one", "raw note two"):
        run_cli("--vault", str(vault), "capture", text, "--project", "wraith")
    curated = run_cli("--vault", str(vault), "write", "--summary", "curated", "--evidence", "proof")
    assert curated.returncode == 0, curated.stderr
    out = run_cli(*argv_from(doc_command(["stats"])), cwd=str(vault))
    assert out.returncode == 0
    report = json.loads(out.stdout)
    assert report["entries"] == 3
    assert report["promotion_rate"] == round(report["promoted"] / report["entries"], 3)


def test_audit_exits_zero_on_a_clean_vault(tmp_path):
    vault = make_vault(tmp_path)
    seeded = run_cli("--vault", str(vault), "capture", "fine note")
    assert seeded.returncode == 0, seeded.stderr
    out = run_cli(*argv_from(doc_command(["audit"])), cwd=str(vault))
    assert out.returncode == 0
    assert json.loads(out.stdout)["problems"] == []


def test_audit_exits_non_zero_with_the_documented_problems(tmp_path):
    """docs/USING.md:142-148 — the dangling id is derived from the doc's own quoted audit output."""
    vault = make_vault(tmp_path)
    deadbeef = doc_quote(r"supersedes missing entry (\w+)").group(1)
    seeded = run_cli("--vault", str(vault), "write", "--state", "promoted", "--summary",
                     "no proof", "--supersedes", deadbeef)
    assert seeded.returncode == 0, seeded.stderr
    out = run_cli(*argv_from(doc_command(["audit"])), cwd=str(vault))
    assert out.returncode != 0
    problems = json.loads(out.stdout)["problems"]
    assert any("promoted without evidence" in p for p in problems)
    assert any(f"supersedes missing entry {deadbeef}" in p for p in problems)


def test_reflect_with_a_transcript_and_fake_reports_the_documented_summary(tmp_path):
    """docs/USING.md:40 + :128 — the documented reflect form (`<transcript>`) running with the
    documented --fake flag needs no model and reports the proposal counts the doc quotes."""
    vault = make_vault(tmp_path)
    fake = tmp_path / "fake.json"
    fake.write_text(json.dumps([
        {"kind": "decision", "summary": "DOCP 3600 is stable again after the BIOS 2601 flash",
         "evidence": "3 consecutive boots, 0 MCE lines each", "importance": 0.95,
         "ask": "why did the box reboot at 03:02?"},
        {"kind": "lesson", "summary": "Always check MCE lines before rolling back a BIOS",
         "evidence": "journalctl -k per boot: 0 MCE lines each", "importance": 0.8,
         "ask": "what did the BIOS rollback teach us?"}]), encoding="utf-8")
    transcript = tmp_path / "session.md"
    transcript.write_text("user: box rebooted at 03:02; BIOS flash investigated\n", encoding="utf-8")
    out = run_cli(*argv_from(doc_command(["--fake", "<file.json>"]),
                             mapping={"<transcript>": str(transcript),
                                      "<file.json>": str(fake)}), cwd=str(vault))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "2 proposals, 2 promoted, 0 left raw"


def test_reflect_documented_fake_form_runs(tmp_path):
    """docs/USING.md's 'No model available' section and README's reflect paragraph document
    `quipu reflect <transcript> --fake <file.json>`; this probe derives that one distinct command
    from both docs and asserts it runs green. HISTORY: both files used to document a bare
    `quipu reflect --fake <file.json>` — a command that cannot produce the output it quotes (a
    transcript or stdin is required) — and the suite held that claim red until it was corrected.
    A regression fails here again: either as an ambiguity listing both distinct forms and their
    files, or as a non-zero exit."""
    vault = make_vault(tmp_path)
    fake = tmp_path / "fake.json"
    fake.write_text(json.dumps([
        {"kind": "decision", "summary": "proposals from the file", "evidence": "proof",
         "importance": 0.9, "ask": "the question this answers?"}]), encoding="utf-8")
    transcript = tmp_path / "transcript.md"
    transcript.write_text("session body\n", encoding="utf-8")
    out = run_cli(*argv_from(doc_command(["--fake", "<file.json>"]),
                             mapping={"<transcript>": str(transcript),
                                      "<file.json>": str(fake)}), cwd=str(vault))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "1 proposals, 1 promoted, 0 left raw"


def test_reflect_dry_run_prints_proposals_and_writes_nothing(tmp_path):
    """docs/USING.md:133 — --dry-run prints the proposals and writes nothing."""
    vault = make_vault(tmp_path)
    fake = tmp_path / "fake.json"
    fake.write_text(json.dumps([
        {"kind": "fact", "summary": "dry run proposal", "evidence": "proof", "importance": 0.9,
         "ask": "the question this answers?"}]), encoding="utf-8")
    transcript = tmp_path / "session.md"
    transcript.write_text("session body\n", encoding="utf-8")
    out = run_cli(*argv_from(doc_command(["--fake", "<file.json>"]),
                             mapping={"<transcript>": str(transcript),
                                      "<file.json>": str(fake)})
                  + ["--dry-run"], cwd=str(vault))
    assert out.returncode == 0
    proposals = json.loads(out.stdout)
    assert [p["summary"] for p in proposals] == ["dry run proposal"]
    assert not any((vault / "notes").glob("*.md")), "dry-run must write nothing"


def test_import_fabric_dry_run_reports_counts(tmp_path):
    """docs/USING.md:45-50 — the documented import command (derived) with --dry-run counts: the
    quoted subset {"dry_run": true, "written": 1, "skipped": 1, by_state promoted: 1}."""
    vault = make_vault(tmp_path)
    src = tmp_path / "corpus"
    src.mkdir()
    (src / "entry.md").write_text(
        '---\nid: "a86b8c2a"\nagent: "cipher"\nplatform: "cli"\n'
        'timestamp: "2026-10-10T18:23:30Z"\ntype: "decision"\ntier: "hot"\n'
        'summary: "one line, the retrieval signal"\nproject_id: "wraith"\n'
        'verified: "true"\nevidence: "the exact command and its output"\n'
        'training_value: "high"\ntags: [wraith, migration]\n---\n\nthe body\n', encoding="utf-8")
    (src / "noisy.md").write_text(
        '---\nid: "11111111"\ntype: "session"\ntraining_value: "low"\n'
        'summary: "fixed a bug"\n---\n\nbody\n', encoding="utf-8")
    out = run_cli(*argv_from(doc_command(["import", "fabric"]), mapping={"<dir>": str(src)})
                  + ["--dry-run"], cwd=str(vault))
    assert out.returncode == 0
    report = json.loads(out.stdout)
    assert report["dry_run"] is True
    assert report["written"] == 1 and report["skipped"] == 1
    assert report["by_state"] == {"promoted": 1}


def test_import_fabric_makes_the_archive_searchable(tmp_path):
    """docs/USING.md:45-46 — an imported corpus is recallable through the same search path."""
    vault = make_vault(tmp_path)
    src = tmp_path / "corpus"
    src.mkdir()
    (src / "entry.md").write_text(
        '---\nid: "a86b8c2a"\ntype: "decision"\ntraining_value: "high"\n'
        'summary: "one line, the retrieval signal"\n---\n\nthe body\n', encoding="utf-8")
    imported = run_cli(*argv_from(doc_command(["import", "fabric"]), mapping={"<dir>": str(src)}),
                       cwd=str(vault))
    assert imported.returncode == 0, imported.stderr
    out = run_cli("--vault", str(vault), "search", "retrieval signal")
    assert out.returncode == 0 and "retrieval signal" in out.stdout


def test_import_include_noise_imports_low_value_as_pruned_with_reason_tag(tmp_path):
    """docs/USING.md:48-50 — low-value session entries: skipped by default; with --include-noise
    they arrive as state=pruned tagged pruned:low-training-value, never as promoted."""
    vault = make_vault(tmp_path)
    src = tmp_path / "corpus"
    src.mkdir()
    (src / "noisy.md").write_text(
        '---\nid: "11111111"\ntype: "session"\ntraining_value: "low"\n'
        'summary: "fixed a bug"\n---\n\nbody\n', encoding="utf-8")
    out = run_cli(*argv_from(doc_command(["import", "fabric"]), mapping={"<dir>": str(src)})
                  + ["--include-noise"], cwd=str(vault))
    assert out.returncode == 0, out.stderr
    report = json.loads(out.stdout)
    assert report["written"] == 1 and report["skipped"] == 0
    entry_files = [p for p in (vault / "notes").glob("*.md")
                   if "fixed a bug" in p.read_text(encoding="utf-8")]
    assert len(entry_files) == 1
    text = entry_files[0].read_text(encoding="utf-8")
    assert "state: pruned" in text
    assert "pruned:low-training-value" in text
    assert "promoted" not in text.replace("state: pruned", "")


def test_show_prints_a_documented_entry(tmp_path):
    """README commands table: `quipu show` — print an entry."""
    vault = make_vault(tmp_path)
    cap = run_cli(*argv_from(doc_command(["capture", "--project"]), vault=vault))
    assert cap.returncode == 0, cap.stderr
    out = run_cli(*argv_from(doc_command(["show"])) + [cap.stdout.strip()], cwd=str(vault))
    assert out.returncode == 0
    assert "DOCP config threw 649 CCEs" in out.stdout


def test_edge_and_neighbors_walk_the_documented_neighbourhood(tmp_path):
    """README table: `quipu edge` asserts; `quipu neighbors <entity> --depth 2` — derived."""
    vault = make_vault(tmp_path)
    first = run_cli("--vault", str(vault), "edge", "quipu", "depends_on", "sqlite")
    second = run_cli("--vault", str(vault), "edge", "sqlite", "used_by", "fts5")
    assert first.returncode == 0 and second.returncode == 0
    out = run_cli(*argv_from(doc_command(["neighbors", "--depth"]),
                             mapping={"<entity>": "quipu"}), cwd=str(vault))
    assert out.returncode == 0
    assert "fts5" in out.stdout, "depth 2 must reach across the hop"