"""Black-box tests for the `quipu mcp` server (#9).

The server is driven exactly as a client would: `python -m quipu --vault <tmp> mcp`, one JSON-RPC
request per line on stdin, one JSON line per response on stdout. Assertions come from the
PROTOCOL SPEC (issue #9), not from the implementation — the server was written by a different
worker, which is exactly why someone else writes its tests.

Hermetic: a tmp vault per test, no network, no model, no other fixtures. Every response read has
a deadline, so a server that never answers fails the test instead of hanging CI, and the fixture
kills its server even on failure — no stray server may sit on a test and report green for a tree
that is not the one under test.

Finding protocol: a failing test is reported with the REQUEST and the ACTUAL RESPONSE line and is
not weakened; the server's owner fixes quipu/mcp.py.
"""

import functools
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: The server to exercise. Defaults to this repo's tree (the merged-world shape); the harness can
#: be pointed at another checkout that owns quipu/mcp.py, because the invariant is the PROTOCOL,
#: not one checkout.
SERVER_PY = os.environ.get("QUIPU_MCP_PY", sys.executable)
SERVER_TREE = Path(os.environ.get("QUIPU_MCP_TREE", str(REPO)))
DEADLINE = 5.0


@functools.cache
def server_tree_version():
    """The version the tree under test would report: derived from THAT tree, not this pytest one."""
    out = subprocess.run([SERVER_PY, "-c", "import sigil; print(sigil.__version__)"],
                         capture_output=True, text=True, timeout=30, cwd=str(SERVER_TREE))
    assert out.returncode == 0, f"the server tree's package does not import: {out.stderr!r}"
    return out.stdout.strip()


class McpServer:
    def __init__(self, vault):
        self.proc = subprocess.Popen(
            [SERVER_PY, "-m", "sigil", "--vault", str(vault), "mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, cwd=str(SERVER_TREE),
            env=dict(os.environ, PYTHONUNBUFFERED="1"))
        self.lines = []
        self.stdout_lines = []
        self.next_id = 0
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self):
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\n"))
            self.stdout_lines.append(line.rstrip("\n"))

    def send(self, raw_or_obj):
        if isinstance(raw_or_obj, str):
            self.proc.stdin.write(raw_or_obj + "\n")
        else:
            self.proc.stdin.write(json.dumps(raw_or_obj) + "\n")
        self.proc.stdin.flush()

    def _pop(self, deadline):
        """One line, JSON-parsed; stdout purity is asserted on every single read."""
        while time.time() < deadline:
            if self.lines:
                raw = self.lines.pop(0)
                return json.loads(raw), raw  # not-a-JSON-line would raise here
            if self.proc.poll() is not None and not self.lines:
                pytest.fail("the server exited while a response was pending "
                            f"(exit code {self.proc.returncode})")
            time.sleep(0.03)
        pytest.fail(f"no response within {DEADLINE}s — a server that never answers fails the "
                    f"test instead of hanging CI (pending: {self.lines[:2]!r})")

    def ask(self, method, params=None):
        """A request; the response must carry the request's id."""
        self.next_id += 1
        request = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            request["params"] = params
        self.send(request)
        response, _raw = self._pop(time.time() + DEADLINE)
        assert response.get("id") == self.next_id, (
            f"response does not answer its request:\n  request: {json.dumps(request)}\n"
            f"  response line: {_raw!r}")
        return response, _raw

    def expect_silence(self, seconds=0.9):
        """A notification must produce no response line."""
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        time.sleep(seconds)
        pending = self.lines[:]
        self.lines = self.lines[len(pending):]
        return pending

    def close_stdin(self):
        self.proc.stdin.close()

    def kill(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass
            self.proc.kill()
            self.proc.wait(timeout=10)


@pytest.fixture()
def srv(tmp_path):
    # The server refuses to start against a path that is not a vault (#141), so the fixture makes one
    # the way a user would: a vault root with its notes directory. Everything else the server derives.
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    server = McpServer(vault)
    try:
        yield server
    finally:
        server.kill()


def _call(srv, name, arguments):
    """tools/call -> (isError, content_text, parsed_json_or_None, raw_line)."""
    response, raw = srv.ask("tools/call", {"name": name, "arguments": arguments})
    result = response.get("result", {})
    content = result.get("content") or []
    text = content[0]["text"] if isinstance(content, list) and content and isinstance(
        content[0], dict) else ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    return response["result"].get("isError", False), text, payload, raw


def _rpc_error(response, raw):
    error = response.get("error")
    assert error is not None, f"expected an error object, got: {raw!r}"
    return error["code"], error


# --- 1. stdout purity + parse error recovery -----------------------------------------


def test_stdout_is_json_only_and_a_bad_line_still_serves(srv):
    srv.send("this is not json at all")
    response, raw = srv._pop(time.time() + DEADLINE)
    code, error = _rpc_error(response, raw)
    assert code == -32700, f"the malformed line must get a parse error: {raw!r}"
    assert response.get("id") is None
    assert "parse" in error["message"].lower()

    after, raw = srv.ask("tools/list")
    assert after.get("result"), f"the server must keep serving after the bad line: {raw!r}"
    # and every stdout line so far parsed as JSON — enforced by _pop on every read


def test_logs_go_to_stderr_not_between_responses(srv):
    for _ in range(3):
        srv.ask("tools/list")
    # every stdout line is a JSON response (each _pop read asserts the parse); the server's own
    # banner may not slip between them
    assert all(line.lstrip().startswith("{") for line in srv.stdout_lines), (
        f"a non-response line slipped onto stdout: {srv.stdout_lines}")


# --- 2. handshake --------------------------------------------------------------------


def test_initialize_handshake_and_initialized_notification(srv):
    response, raw = srv.ask("initialize", {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "mcp-tests", "version": "0"}})
    result = response.get("result")
    assert result is not None, f"initialize must answer: {raw!r}"
    assert isinstance(result["protocolVersion"], str) and result["protocolVersion"], raw
    assert "tools" in result.get("capabilities", {}), (
        f"capabilities must advertise tools: {json.dumps(result['capabilities'])}")
    info = result["serverInfo"]
    assert info["name"] == "sigil", f"serverInfo.name: {info!r}"
    assert info["version"] == server_tree_version(), (
        f"serverInfo.version must match the package's __version__: {info!r}")
    # notifications/initialized produces NO response line
    pending = srv.expect_silence()
    assert pending == [], f"a notification must not be answered: {pending!r}"


# --- 3. tools/list -------------------------------------------------------------------


def test_tools_list_exactly_the_five_documented(srv):
    response, raw = srv.ask("tools/list")
    tools = response["result"]["tools"]
    names = [t["name"] for t in tools]
    assert names == ["search", "timeline", "neighbors", "write", "stats"], (
        f"tools/list: {names}")
    expected_required = {"search": ["query"], "timeline": ["entity"], "neighbors": ["entity"],
                         "write": ["summary"]}
    for tool in tools:
        schema = tool["inputSchema"]
        assert schema.get("type") == "object", f"{tool['name']} inputSchema: {schema}"
        if tool["name"] in expected_required:
            required = schema.get("required")
            assert isinstance(required, list) and required, f"{tool['name']} schema: {schema}"
            for arg in expected_required[tool["name"]]:
                assert arg in required, (
                    f"{tool['name']}.required lacks {arg!r}: {schema}")
        # stats takes no arguments: 'required where it makes sense' means it must not demand any
        if tool["name"] == "stats":
            assert not schema.get("required"), f"stats must require nothing: {schema}"


# --- 4. protocol errors, and the server survives them --------------------------------


def test_unknown_method_is_minus_32601_and_the_server_survives(srv):
    response, raw = srv.ask("no/such/method")
    code, _error = _rpc_error(response, raw)
    assert code == -32601, f"unknown method must be -32601: {raw!r}"
    after, _ = srv.ask("tools/list")
    assert after.get("result"), "the server must still answer after the protocol error"


def test_unknown_tool_is_an_error_not_a_crash(srv):
    response, raw = srv.ask("tools/call", {"name": "no_such_tool", "arguments": {}})
    result = response.get("result")
    if result is not None:
        assert result.get("isError") is True, f"unknown tool must error cleanly: {raw!r}"
    else:
        code, _error = _rpc_error(response, raw)
        assert code in (-32602, -32601), f"unknown tool error code: {raw!r}"
    after, _ = srv.ask("tools/list")
    assert after.get("result"), "the server must not crash on an unknown tool"


def test_a_batch_array_is_minus_32600(srv):
    srv.send([{"jsonrpc": "2.0", "id": 999, "method": "tools/list"}])
    response, raw = srv._pop(time.time() + DEADLINE)
    code, _error = _rpc_error(response, raw)
    assert code == -32600, f"a batch array must be rejected: {raw!r}"
    after, _ = srv.ask("tools/list")
    assert after.get("result"), "the server must still answer after the batch"


# --- 5. write -> search round trip ----------------------------------------------------


def test_write_then_search_round_trips_to_the_first_hit(srv):
    _, text, written, _ = _call(srv, "write", {
        "summary": "TRX40 needs DOCP 3200 for stability", "evidence": "3 boots, zero MCE lines",
        "ask": "why did the box reboot at 03:02?", "kind": "decision", "state": "promoted",
        "edges": ["TRX40:measured_as:DOCP 3200"]})
    assert written is not None, f"the write result must parse: {text!r}"
    assert written["indexed"] is True
    eid = written["id"]
    path = Path(written["path"])

    _, text, hits, _ = _call(srv, "search", {"query": "DOCP 3200"})
    assert hits["hits"], f"the written entry must be findable: {text!r}"
    assert hits["hits"][0]["id"] == eid, (
        f"the entry must be the FIRST hit with the same id: {json.dumps(hits['hits'][:1])}")

    assert path.exists(), f"the entry file must exist on disk: {path}"
    md = path.read_text(encoding="utf-8")
    assert "state: promoted" in md, f"the requested state must be in the frontmatter:\n{md[:400]}"
    assert "TRX40:measured_as:DOCP 3200" in md, \
        "the requested edge must be recorded in the entry"


# --- 6. a bad edge spec must not cost the memory --------------------------------------


def test_write_with_malformed_and_out_of_vocab_edges_keeps_the_entry(srv):
    for summary, spec, phrase in (
            ("probe entry with a malformed claim", "onlytwo:parts", "malformed claim"),
            ("probe entry with a wild predicate", "ALPHA:likes:BETA", "wild predicate")):
        error, text, written, _ = _call(srv, "write", {
            "summary": summary, "state": "promoted", "edges": [spec]})
        assert error is False and written is not None, \
            f"a bad spec must not error the write: {text!r}"
        assert written["indexed"] is True, f"the entry must be indexed anyway: {text!r}"
        assert written.get("skipped_edges") and any(
            spec in item for item in written["skipped_edges"]), (
            f"skipped_edges must name the rejected spec {spec!r}: {text!r}")
        _, text, hits, _ = _call(srv, "search", {"query": phrase})
        assert hits["hits"], f"the entry must stay findable by search: {text!r}"
        assert hits["hits"][0]["id"] == written["id"]


# --- 7. write with no summary ---------------------------------------------------------


def test_write_with_no_summary_is_a_readable_error(srv):
    error, text, payload, _ = _call(srv, "write", {"state": "promoted"})
    assert error is True, f"no summary must be isError: {text!r}"
    assert payload is None, f"the error must not be a wrapped result: {text!r}"
    assert text, "the message must be readable text"
    assert "summary" in text.lower(), f"the message must say what is missing: {text!r}"


# --- 8. timeline / neighbors / stats --------------------------------------------------


def test_graph_tools_return_the_documented_structures(srv):
    _, text, seeded, _ = _call(srv, "write", {
        "summary": "seed edge for graph structures", "edges": ["TRX40:measured_as:DOCP 3200"]})
    assert seeded["indexed"] is True

    _, text, tl, _ = _call(srv, "timeline", {"entity": "TRX40"})
    assert tl is not None and tl["entity"] == "TRX40", f"timeline: {text!r}"
    assert len(tl["edges"]) == 1, f"the asserted edge must be in the timeline: {text!r}"
    assert tl["edges"][0]["dst"] == "docp-3200" and tl["edges"][0]["valid_to"] is None

    _, text, nb, _ = _call(srv, "neighbors", {"entity": "TRX40", "depth": 2})
    assert nb is not None, f"neighbors: {text!r}"
    names = {n["name"] for n in nb["nodes"]}
    assert {"trx40", "docp-3200"} <= names, f"the neighbourhood must contain both nodes: {text!r}"
    assert nb["edges"], "the asserted edge must be in the neighbourhood"

    _, text, st, _ = _call(srv, "stats", {})
    assert st is not None, f"stats: {text!r}"
    assert st["entries"] >= 1 and st["edges_live"] >= 1, f"stats must reflect the graph: {text!r}"


def test_unknown_entity_returns_empty_not_an_error(srv):
    for name in ("timeline", "neighbors"):
        error, text, payload, _ = _call(srv, name, {"entity": "NO-SUCH-ENTITY"})
        assert error is False, f"{name} on an unknown entity raised: {text!r}"
        assert payload is not None
        assert payload["edges"] == [], f"{name} on an unknown entity must be empty: {text!r}"
    _, text, st, _ = _call(srv, "stats", {})
    assert st is not None, f"stats must still work: {text!r}"


# --- 9. EOF ---------------------------------------------------------------------------


def test_closing_stdin_exits_zero(srv):
    srv.ask("tools/list")
    srv.close_stdin()
    code = srv.proc.wait(timeout=10)
    assert code == 0, f"EOF must exit 0, got {code}"
    stderr = srv.proc.stderr.read()
    assert stderr, "the server must log its banner on stderr, not stdout"
    assert "serving" in stderr, f"unexpected stderr: {stderr[:200]!r}"