"""The COre <-> Cortex contract: structured results, actor identity, change-sets and undo.

These run the real MCP server (in-process and over stdio) the way COre's memgate.js calls it,
and check both the new structured output and that COre's existing text regexes still work."""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["CORTEX_SEMANTIC"] = "off"
os.environ.setdefault("CORTEX_LOCAL_DIR", tempfile.mkdtemp(prefix="cortex-test-local-"))

from cortex import __version__  # noqa: E402
from cortex.core import Brain  # noqa: E402
from cortex.mcp_server import CONTRACT, TOOLS, Server  # noqa: E402

# COre's parsers, copied from src/main/memgate.js and opportunities.js
CORE_PATH_RE = re.compile(r"^## (.+)$", re.M)
CORE_TASK_RE = re.compile(r"^- \[ \] (.+?)(?: \(due (\d{4}-\d{2}-\d{2})(?:, OVERDUE)?\))? — (.+)$")


def check(schema, value, where="$"):
    """A small JSON-schema checker for the subset Cortex uses (type, properties, items)."""
    t = schema.get("type")
    types = t if isinstance(t, list) else [t] if t else []
    py = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float),
          "boolean": bool, "null": type(None)}
    if types and not any(isinstance(value, py[x]) and not (x in ("integer", "number") and isinstance(value, bool))
                         for x in types):
        raise AssertionError(f"{where}: expected {types}, got {type(value).__name__}")
    if isinstance(value, dict):
        for k, sub in schema.get("properties", {}).items():
            if k in value:
                check(sub, value[k], f"{where}.{k}")
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            check(schema["items"], v, f"{where}[{i}]")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = Brain(Path(self.tmp.name) / "v")
        self.s = Server(self.b, "core")
        self.n = 0

    def tearDown(self):
        self.b.close()
        self.tmp.cleanup()

    def call(self, name, **args):
        self.n += 1
        r = self.s.handle({"jsonrpc": "2.0", "id": self.n, "method": "tools/call",
                           "params": {"name": name, "arguments": args}})["result"]
        schema = next(t for t in TOOLS if t["name"] == name).get("outputSchema")
        if not r.get("isError"):
            self.assertIn("structuredContent", r, name)
            check(schema, r["structuredContent"], name)
        return r


class ContractTest(Base):
    def test_initialize_reports_version_and_contract(self):
        r = Server(self.b).handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "core"}}})["result"]
        self.assertEqual(r["serverInfo"]["version"], __version__)
        self.assertEqual(r["capabilities"]["experimental"]["cortex"]["contract"], CONTRACT)
        self.assertIn("undo", r["capabilities"]["experimental"]["cortex"]["features"])

    def test_every_tool_declares_an_output_schema(self):
        self.assertEqual([t["name"] for t in TOOLS if "outputSchema" not in t], [])

    def test_recall_headings_are_clean_paths(self):
        self.b.write("Projects/Website.md", "# Website\nThe launch is in March. See [[Hosting]].", agent="you")
        self.b.write("Hosting.md", "# Hosting\nWe host on a small VPS for the launch.\n```\n## not a source\n```", agent="you")
        r = self.call("brain_recall", query="launch hosting", budget=800, hops=1)
        paths = CORE_PATH_RE.findall(r["content"][0]["text"])
        self.assertTrue(paths)
        for p in paths:
            self.assertTrue(p.endswith(".md"), p)  # nothing appended after the path
        self.assertNotIn("not a source", paths)
        sc = r["structuredContent"]
        self.assertEqual({p["path"] for p in sc["passages"]}, set(paths))
        self.assertTrue(all(p["kind"] == "note" for p in sc["passages"]))

    def test_tasks_parse_with_cores_regex_and_structured(self):
        self.b.add_task("Pay rent", due="2020-01-01", agent="you")
        self.b.add_task("Read A — B chapter", agent="you")
        r = self.call("brain_tasks", action="list", status="open")
        lines = r["content"][0]["text"].splitlines()
        self.assertTrue(all(CORE_TASK_RE.match(l) for l in lines), lines)
        sc = {t["text"].split(" 📅")[0]: t for t in r["structuredContent"]["tasks"]}
        self.assertTrue(sc["Pay rent"]["overdue"])
        self.assertEqual(sc["Read A — B chapter"]["due"], None)

    def test_log_accepts_any_short_kind(self):
        self.assertFalse(self.call("brain_log", summary="weekly tidy", kind="maintenance").get("isError"))
        self.assertTrue(self.call("brain_log", summary="x", kind="not a word!").get("isError"))
        self.assertEqual(self.b.activity(1)[0]["action"], "maintenance")
        self.assertIn("weekly tidy", json.dumps(self.b.recap(1)["decisions_and_work"]))

    def test_health_summary_line(self):
        self.b.write("A.md", "# A\nsee [[Missing]] and some more words here", agent="you")
        r = self.call("brain_health")
        first = r["content"][0]["text"].splitlines()[0]
        self.assertRegex(first, r"^Health: (ok|\d+ issues? \(.*\))$")
        self.assertIn("broken links 1", first)
        self.assertFalse(r["structuredContent"]["ok"])

    def test_actor_is_logged_and_cannot_impersonate(self):
        self.call("brain_capture", text="hello", actor="forge")
        self.assertEqual(self.b.activity(1)[0]["agent"], "core/forge")
        self.assertTrue(self.call("brain_capture", text="x", actor="you").get("isError"))
        self.assertTrue(self.call("brain_capture", text="x", actor="Bad Name!").get("isError"))
        self.assertEqual(self.b.activity(5, agent="core")[0]["agent"], "core/forge")  # prefix match

    def test_expect_mtime_conflict(self):
        r = self.call("brain_write", path="N.md", content="# N\none", mode="create")
        mt = r["structuredContent"]["mtime"]
        time.sleep(0.01)
        self.b.write("N.md", "# N\nchanged by the user", agent="you")
        r2 = self.call("brain_edit", path="N.md", find="changed", replace="edited", expect_mtime=mt)
        self.assertTrue(r2.get("isError"))
        self.assertIn("changed by someone else", r2["content"][0]["text"])


class UndoTest(Base):
    def test_undo_write_create_and_redo(self):
        self.b.write("N.md", "# N\noriginal", agent="you")
        r = self.call("brain_write", path="N.md", content="added by core", actor="agent")
        cs = r["structuredContent"]["changeset"]
        self.call("brain_write", path="New.md", content="# New\nfresh", mode="create", actor="agent")
        u = self.call("brain_undo", last_by="core/agent")["structuredContent"]
        self.assertEqual(u["reverted"], ["New.md"])
        self.assertFalse((self.b.root / "New.md").exists())
        u = self.call("brain_undo", changeset=cs)["structuredContent"]
        self.assertEqual((self.b.root / "N.md").read_text(), "# N\noriginal\n")
        redo = self.call("brain_undo", changeset=u["changeset"])["structuredContent"]  # undo the undo
        self.assertIn("added by core", (self.b.root / "N.md").read_text())
        self.assertEqual(redo["reverted"], ["N.md"])

    def test_undo_skips_files_changed_again(self):
        r = self.call("brain_write", path="N.md", content="# N\nby core", mode="create")
        self.b.write("N.md", "# N\nthe user rewrote it", agent="you")
        u = self.call("brain_undo", changeset=r["structuredContent"]["changeset"])["structuredContent"]
        self.assertEqual(u["reverted"], [])
        self.assertEqual(u["skipped"][0]["reason"], "changed again since")
        self.assertIn("user rewrote", (self.b.root / "N.md").read_text())

    def test_undo_move_restores_links(self):
        self.b.write("Old.md", "# Old\nbody", agent="you")
        self.b.write("Ref.md", "# Ref\nsee [[Old]]", agent="you")
        r = self.call("brain_move", path="Old.md", to="Archive/New.md")
        self.assertIn("[[New]]", (self.b.root / "Ref.md").read_text())
        u = self.call("brain_undo", changeset=r["structuredContent"]["changeset"])["structuredContent"]
        self.assertEqual(sorted(u["reverted"]), ["Old.md", "Ref.md"])
        self.assertTrue((self.b.root / "Old.md").exists())
        self.assertIn("[[Old]]", (self.b.root / "Ref.md").read_text())

    def test_atomic_batch_rolls_back(self):
        self.b.write("P.md", "# P\nkeep", agent="you")
        r = self.call("brain_batch", atomic=True, ops=[
            {"op": "section", "path": "P.md", "section": "Decisions", "content": "- chose A", "mode": "append"},
            {"op": "write", "path": "Q.md", "content": "# Q", "mode": "create"},
            {"op": "edit", "path": "P.md", "find": "no such text", "replace": "x"}])
        sc = r["structuredContent"]
        self.assertTrue(sc["rolled_back"])
        self.assertEqual((self.b.root / "P.md").read_text(), "# P\nkeep\n")
        self.assertFalse((self.b.root / "Q.md").exists())

    def test_batch_is_one_changeset(self):
        r = self.call("brain_batch", ops=[{"op": "write", "path": "A.md", "content": "# A", "mode": "create"},
                                          {"op": "task", "text": "Ship", "path": "A.md"}])
        cs = r["structuredContent"]["changeset"]
        self.assertEqual(self.b.changesets(1)[0]["id"], cs)
        self.call("brain_undo", changeset=cs)
        self.assertFalse((self.b.root / "A.md").exists())

    def test_dry_run_changes_nothing(self):
        self.b.write("P.md", "# P\nkeep", agent="you")
        before = (self.b.root / "P.md").read_text()
        r = self.call("brain_batch", dry_run=True, ops=[
            {"op": "section", "path": "P.md", "section": "Decisions", "content": "- chose A", "mode": "append"},
            {"op": "edit", "path": "P.md", "find": "chose A", "replace": "chose B"},  # sees op 1's effect
            {"op": "write", "path": "_CORTEX.md", "content": "evil", "mode": "overwrite"}])
        sc = r["structuredContent"]
        self.assertEqual([x["ok"] for x in sc["results"]], [True, True, False])
        self.assertIn("+- chose B", sc["results"][1]["diff"])
        self.assertEqual((self.b.root / "P.md").read_text(), before)
        self.assertEqual(self.b.changesets(5), [c for c in self.b.changesets(5) if c["actor"] == "you"])

    def test_history_and_as_of(self):
        self.b.write("N.md", "# N\nv1", agent="you")
        time.sleep(1.1)
        mid = time.strftime("%Y-%m-%dT%H:%M:%S")
        time.sleep(1.1)
        self.b.write("N.md", "# N\nv2", agent="you")
        h = self.call("brain_history", path="N.md")["structuredContent"]
        self.assertEqual(len(h["versions"]), 1)
        r = self.call("brain_read", path="N.md", as_of=mid)["structuredContent"]
        self.assertIn("v1", r["content"])
        self.assertIn("v2", self.call("brain_read", path="N.md")["structuredContent"]["content"])
        sets = self.call("brain_history")["structuredContent"]["changesets"]
        self.assertGreaterEqual(len(sets), 2)

    def test_ai_cannot_undo_the_users_rules_change(self):
        self.b.write("_CORTEX.md", "# Rules\nv1", agent="you")
        self.b.write("_CORTEX.md", "# Rules\nv2", agent="you")
        u = self.call("brain_undo")["structuredContent"]
        self.assertEqual(u["reverted"], [])
        self.assertIn("only the user", u["skipped"][0]["reason"])


class StdioTest(unittest.TestCase):
    """COre's real call sequence over a real subprocess (as cortex.js spawns it)."""

    def test_memgate_sequence(self):
        with tempfile.TemporaryDirectory() as t:
            env = {**os.environ, "CORTEX_AGENT": "core", "CORTEX_AUTO_IMPORT": "off", "PYTHONPATH": str(ROOT)}
            p = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "mcp", "--vault", str(Path(t) / "v")], env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                 encoding="utf-8")
            calls = [("initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "core", "version": "2.1"}})]
            tools = [("brain_edit", {"path": "Projects/Alpha.md", "section": "Decisions", "content": "- chose A", "mode": "append"}),
                     ("brain_capture", {"text": "lesson: test first"}),
                     ("brain_recall", {"query": "alpha decisions", "budget": 300, "hops": 1}),
                     ("brain_search", {"query": "chose A", "scope": "notes", "limit": 5}),
                     ("brain_read", {"path": "Projects/Alpha.md"}),
                     ("brain_log", {"summary": "tidy", "kind": "maintenance", "target": "vault"}),
                     ("brain_tasks", {"action": "list", "status": "open"}),
                     ("brain_health", {})]
            calls += [("tools/call", {"name": n, "arguments": a}) for n, a in tools]
            out = []
            for i, (m, params) in enumerate(calls, 1):
                p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": m, "params": params}) + "\n")
                p.stdin.flush()
                out.append(json.loads(p.stdout.readline()))
            p.stdin.close()
            p.wait(10)
            p.stdout.close()
            for (m, params), r in zip(calls, out):
                self.assertIn("result", r, r)
                if m == "tools/call":
                    self.assertFalse(r["result"].get("isError"), (params["name"], r))
                    self.assertIn("structuredContent", r["result"])
            recall = out[3]["result"]
            self.assertEqual(CORE_PATH_RE.findall(recall["content"][0]["text"])[0], "Projects/Alpha.md")


if __name__ == "__main__":
    unittest.main()
