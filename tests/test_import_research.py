"""Tests for conversation import (every supported app) and the free research sources.

Research sources are tested against sample responses in each service's documented
format (the network is mocked), so these tests never depend on the internet.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["CORTEX_SEMANTIC"] = "off"
os.environ.setdefault("CORTEX_LOCAL_DIR", tempfile.mkdtemp(prefix="cortex-test-local-"))

from cortex import importers as I  # noqa: E402
from cortex import research as R  # noqa: E402
from cortex.core import Brain  # noqa: E402


def jl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.vault = Path(self.tmp.name) / "vault"
        self.patch = mock.patch.object(I, "HOME", self.home)
        self.patch.start()
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ.pop("CODEX_HOME", None)
        self.b = Brain(self.vault)

    def tearDown(self):
        self.b.close()
        self.patch.stop()
        self.tmp.cleanup()

    def notes(self, source):
        return sorted((self.vault / "Conversations" / source).rglob("*.md"))

    def test_claude_code(self):
        f = self.home / ".claude/projects/-home-me-app/abc123def456.jsonl"
        jl(f, [
            {"type": "user", "timestamp": "2026-11-01T09:00:00Z", "cwd": "/home/me/app", "sessionId": "s",
             "message": {"role": "user", "content": "<system-reminder>ignore me</system-reminder>Fix the login bug please"}},
            {"type": "assistant", "timestamp": "2026-11-01T09:00:05Z", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "secret reasoning"},
                {"type": "text", "text": "Looking at it."},
                {"type": "tool_use", "name": "Bash", "input": {"command": "npm test -- login"}}]}},
            {"type": "user", "timestamp": "2026-11-01T09:00:09Z", "message": {"role": "user", "content": [
                {"type": "tool_result", "content": "HUGE TOOL OUTPUT " * 500}]}},
            {"type": "assistant", "timestamp": "2026-11-01T09:00:12Z", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Fixed: the token check was inverted. My key is " + "sk-ant-" + "api03-abcdefghijklmnopqrstuvwxyz0123"}]}},
            {"type": "user", "isSidechain": True, "timestamp": "2026-11-01T09:01:00Z", "message": {"role": "user", "content": "subagent noise"}},
            {"type": "user", "timestamp": "2026-11-01T09:02:00Z", "message": {"role": "user", "content":
                "<command-name>/model</command-name><command-args>opus</command-args>"}},
            {"type": "summary", "summary": "Login bug fix"},
        ])
        r = I.Importer(self.b).run()
        self.assertEqual(r["new"], 1)
        [n] = self.notes("Claude Code")
        t = n.read_text()
        self.assertIn("Login bug fix", n.name)
        self.assertIn('project: "/home/me/app"', t)
        self.assertIn("Fix the login bug please", t)
        self.assertNotIn("ignore me", t)
        self.assertNotIn("secret reasoning", t)
        self.assertNotIn("HUGE TOOL OUTPUT", t)
        self.assertNotIn("subagent noise", t)
        self.assertIn("> used Bash: `npm test -- login`", t)
        self.assertIn("Looking at it.\n> used Bash", t)  # one assistant turn, merged
        self.assertIn("/model opus", t)
        self.assertIn("[REDACTED]", t)
        self.assertNotIn("sk-ant-api03", t)
        # incremental: unchanged -> skipped; appended -> same note updated
        self.assertEqual(I.Importer(self.b).run()["new"], 0)
        with open(f, "a") as fh:
            fh.write(json.dumps({"type": "user", "timestamp": "2026-11-01T10:00:00Z",
                                 "message": {"role": "user", "content": "One more thing: add a test"}}) + "\n")
        os.utime(f, (os.path.getmtime(f) + 5,) * 2)
        r = I.Importer(self.b).run()
        self.assertEqual((r["new"], r["updated"]), (0, 1))
        self.assertEqual(len(self.notes("Claude Code")), 1)
        self.assertIn("add a test", self.notes("Claude Code")[0].read_text())
        hits = self.b.search("token check inverted", scope="conversations")
        self.assertEqual(hits[0].path, str(n.relative_to(self.vault)).replace(os.sep, "/"))
        self.assertEqual(self.b.search("token check inverted", scope="notes"), [])

    def test_codex_new_and_old_formats(self):
        jl(self.home / ".codex/sessions/2026/11/02/rollout-2026-11-02T10-00-00-aaaa1111.jsonl", [
            {"timestamp": "2026-11-02T10:00:00Z", "type": "session_meta", "payload": {"id": "aaaa1111", "timestamp": "2026-11-02T10:00:00Z", "cwd": "/w"}},
            {"timestamp": "2026-11-02T10:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "<environment_context>cwd=/w</environment_context>"}]}},
            {"timestamp": "2026-11-02T10:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "Refactor the parser"}]}},
            {"timestamp": "2026-11-02T10:00:03Z", "type": "event_msg", "payload": {"type": "user_message", "message": "Refactor the parser"}},
            {"timestamp": "2026-11-02T10:00:04Z", "type": "response_item", "payload": {"type": "function_call", "name": "shell",
             "arguments": json.dumps({"command": ["bash", "-lc", "rg parse"]})}},
            {"timestamp": "2026-11-02T10:00:09Z", "type": "response_item", "payload": {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "Done, split into two functions."}]}},
        ])
        jl(self.home / ".codex/sessions/2025/03/01/rollout-old.jsonl", [
            {"id": "old1", "timestamp": "2025-03-01T08:00:00Z", "instructions": ""},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Old style question"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Old style answer"}]},
        ])
        self.assertEqual(I.Importer(self.b).run()["by_source"], {"Codex": 2})
        texts = "\n".join(n.read_text() for n in self.notes("Codex"))
        self.assertIn("Refactor the parser", texts)
        self.assertEqual(texts.count("Refactor the parser"), 2)  # title + message, event_msg duplicate dropped
        self.assertNotIn("environment_context", texts)
        self.assertIn("> used shell: `bash -lc rg parse`", texts)
        self.assertIn("**Codex**", texts)
        self.assertIn("Old style answer", texts)

    def test_gemini_cli(self):
        chat = self.home / ".gemini/tmp/hash1/chats/session-2026-11-03.json"
        chat.parent.mkdir(parents=True)
        chat.write_text(json.dumps({"sessionId": "g1", "projectHash": "hash1", "startTime": "2026-11-03T12:00:00Z",
                                    "lastUpdated": "2026-11-03T12:05:00Z", "messages": [
            {"id": "1", "timestamp": "2026-11-03T12:00:00Z", "type": "user", "content": "Explain FTS5"},
            {"id": "2", "timestamp": "2026-11-03T12:00:04Z", "type": "gemini", "content": "FTS5 is SQLite's full-text engine.",
             "toolCalls": [{"name": "google_web_search", "args": {"query": "sqlite fts5"}}]},
            {"id": "3", "type": "info", "content": "ignored"}]}))
        logs = self.home / ".gemini/tmp/hash2/logs.json"
        logs.parent.mkdir(parents=True)
        logs.write_text(json.dumps([{"sessionId": "g2", "messageId": 0, "type": "user", "message": "older prompt only",
                                     "timestamp": "2026-10-01T08:00:00Z"}]))
        self.assertEqual(I.Importer(self.b).run()["by_source"], {"Gemini CLI": 2})
        texts = "\n".join(n.read_text() for n in self.notes("Gemini CLI"))
        self.assertIn("**Gemini**", texts)
        self.assertIn("> used google_web_search: `sqlite fts5`", texts)
        self.assertIn("older prompt only", texts)
        self.assertNotIn("ignored", texts)

    def test_cursor(self):
        db_path = self.home / ".config/Cursor/User/globalStorage/state.vscdb"
        if os.name == "nt":
            self.skipTest("path differs on Windows")
        db_path.parent.mkdir(parents=True)
        db = sqlite3.connect(db_path)
        db.execute("CREATE TABLE cursorDiskKV(key TEXT, value TEXT)")
        db.execute("INSERT INTO cursorDiskKV VALUES(?,?)", ("composerData:c1", json.dumps({
            "composerId": "c1", "name": "Add dark mode", "createdAt": 1762000000000, "lastUpdatedAt": 1762000100000,
            "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1}, {"bubbleId": "b2", "type": 2}]})))
        db.execute("INSERT INTO cursorDiskKV VALUES(?,?)", ("bubbleId:c1:b1", json.dumps({"type": 1, "text": "Add a dark mode toggle"})))
        db.execute("INSERT INTO cursorDiskKV VALUES(?,?)", ("bubbleId:c1:b2", json.dumps({"type": 2, "text": "Added it in settings.tsx"})))
        db.execute("INSERT INTO cursorDiskKV VALUES(?,?)", ("composerData:c2", json.dumps({
            "composerId": "c2", "createdAt": 1761000000000, "conversation": [{"type": 1, "text": "Old cursor format"}, {"type": 2, "text": "ok"}]})))
        db.commit()
        db.close()
        with mock.patch.object(I.sys, "platform", "linux"):
            self.assertEqual(I.Importer(self.b).run()["by_source"], {"Cursor": 2})
        texts = "\n".join(n.read_text() for n in self.notes("Cursor"))
        self.assertIn("# Add dark mode", texts)
        self.assertIn("Added it in settings.tsx", texts)
        self.assertIn("Old cursor format", texts)

    def test_chatgpt_export_zip(self):
        conv = {"id": "cg1", "title": "Trip planning", "create_time": 1761900000.0, "update_time": 1761900500.0,
                "current_node": "n3", "mapping": {
                    "root": {"message": None, "parent": None},
                    "n1": {"parent": "root", "message": {"author": {"role": "system"}, "content": {"content_type": "text", "parts": ["sys"]}}},
                    "n2": {"parent": "n1", "message": {"author": {"role": "user"}, "create_time": 1761900001,
                                                       "content": {"content_type": "text", "parts": ["Plan 3 days in Penang"]}}},
                    "alt": {"parent": "n1", "message": {"author": {"role": "user"}, "content": {"content_type": "text", "parts": ["abandoned branch"]}}},
                    "n3": {"parent": "n2", "message": {"author": {"role": "assistant"}, "create_time": 1761900009,
                                                       "content": {"content_type": "text", "parts": ["Day 1: George Town street food."]}}}}}
        z = Path(self.tmp.name) / "export.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("conversations.json", json.dumps([conv]))
        r = I.Importer(self.b).run(export=z)
        self.assertEqual(r["by_source"], {"ChatGPT": 1})
        t = self.notes("ChatGPT")[0].read_text()
        self.assertIn("Plan 3 days in Penang", t)
        self.assertIn("**ChatGPT**", t)
        self.assertNotIn("abandoned branch", t)
        self.assertNotIn("sys", t.split("---")[-1].split("\n", 2)[-1][:5])
        self.assertEqual(I.Importer(self.b).run(export=z)["new"], 0)  # re-import is idempotent

    def test_claude_ai_export(self):
        f = Path(self.tmp.name) / "conversations.json"
        f.write_text(json.dumps([{"uuid": "ca1", "name": "Essay feedback", "created_at": "2026-10-10T10:00:00Z",
                                  "updated_at": "2026-10-10T10:30:00Z", "chat_messages": [
            {"sender": "human", "text": "Review my essay", "created_at": "2026-10-10T10:00:00Z", "content": [{"type": "text", "text": "Review my essay"}]},
            {"sender": "assistant", "text": "Strong opening.", "created_at": "2026-10-10T10:00:10Z"}]}]))
        self.assertEqual(I.Importer(self.b).run(export=f)["by_source"], {"Claude.ai": 1})
        self.assertIn("Strong opening.", self.notes("Claude.ai")[0].read_text())

    def test_bad_files_dont_stop_import(self):
        jl(self.home / ".claude/projects/p/good.jsonl", [{"type": "user", "message": {"role": "user", "content": "hello there"}}])
        (self.home / ".claude/projects/p/bad.jsonl").write_bytes(b"\x00\xff not json at all")
        with self.assertRaises(ValueError):
            I.parse_export(self.home / ".claude/projects/p/good.jsonl")
        self.assertEqual(I.Importer(self.b).run()["new"], 1)

    def test_import_lock(self):
        imp = I.Importer(self.b)
        imp.lock_file.write_text("123")
        self.assertIn("skipped", imp.run())

    def test_redaction(self):
        s = ("ghp_" + "a" * 36 + " AKIAIOSFODNN7EXAMPLE password=hunter2pass Bearer abcdefghijklmnopqrstuvwxyz123 "
             "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY----- normal text sk-proj-" + "b" * 30)
        out = I.redact(s)
        for leaked in ("ghp_aaa", "AKIAIOSFODNN7", "hunter2pass", "abcdefghijklmnopqrstuvwxyz123", "MIIabc", "sk-proj-bbb"):
            self.assertNotIn(leaked, out)
        self.assertIn("normal text", out)
        self.assertIn("password=[REDACTED]", out)


# ── research sources, against sample responses in each service's format ─────
DDG = '''<div class="result"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fsecond-brain&amp;rut=x">The <b>Second Brain</b> guide</a>
<a class="result__snippet" href="#">How to build a <b>second brain</b> with notes.</a></div>
<div class="result"><a class="result__a" href="https://duckduckgo.com/y.js?ad=1">Ad</a></div>
<div class="result"><a class="result__a" href="https://other.org/page">Other page</a><a class="result__snippet">Another snippet</a></div>'''
NEWS = '''<?xml version="1.0"?><rss><channel><item><title>Claude Code adds memory</title><link>https://news.example/1</link>
<pubDate>Sat, 08 Nov 2026 10:00:00 GMT</pubDate><description>&lt;a&gt;Big update&lt;/a&gt;</description><source url="x">Tech Daily</source></item></channel></rss>'''
ARXIV = '''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2601.00001</id>
<title>Memory for  Agents</title><summary>We study long-term memory.</summary><published>2026-01-02T00:00:00Z</published>
<author><name>A. Researcher</name></author></entry></feed>'''
JSONS = {
    "en.wikipedia.org": {"query": {"search": [{"title": "Personal knowledge management", "snippet": "<span>PKM</span> is...", "timestamp": "2026-01-01T00:00:00Z"}]}},
    "hn.algolia.com/api/v1/search": {"hits": [{"title": "Show HN: My second brain", "url": "https://sb.dev", "objectID": "42", "points": 321, "num_comments": 99, "author": "pg", "created_at": "2026-10-01T00:00:00Z"}]},
    "www.reddit.com/search.json": {"data": {"children": [{"data": {"title": "Obsidian vs Notion", "permalink": "/r/ObsidianMD/comments/abc/x/", "selftext": "Which one?", "created_utc": 1762000000, "author": "u1", "score": 150, "subreddit": "ObsidianMD", "num_comments": 80}}]}},
    "api.stackexchange.com": {"items": [{"title": "FTS5 &amp; prefix", "link": "https://so/1", "body": "<p>How do I...</p>", "creation_date": 1700000000, "owner": {"display_name": "dev"}, "score": 12, "is_answered": True, "answer_count": 2}]},
    "public.api.bsky.app/xrpc/app.bsky.feed.searchPosts": {"posts": [{"uri": "at://did:plc:x/app.bsky.feed.post/3kabc", "author": {"handle": "alice.bsky.social"}, "record": {"text": "Loving my AI second brain", "createdAt": "2026-11-01T00:00:00Z"}, "likeCount": 40, "repostCount": 3, "replyCount": 2}]},
    "mastodon.social": [{"content": "<p>New <b>Obsidian</b> plugin</p>", "url": "https://mastodon.social/@bob/1", "created_at": "2026-11-02T00:00:00Z", "account": {"acct": "bob"}, "favourites_count": 7, "reblogs_count": 1}],
    "lemmy.world": {"posts": [{"post": {"name": "PKM tools thread", "ap_id": "https://lemmy.world/post/1", "body": "Share yours", "published": "2026-10-20T00:00:00Z"}, "counts": {"score": 55, "comments": 30}, "creator": {"name": "lem"}, "community": {"name": "pkm"}}]},
    "api.fxtwitter.com": {"tweet": {"text": "Shipping our AI memory feature today", "author": {"name": "Jane", "screen_name": "jane"}, "likes": 1200, "retweets": 80, "replies": 45, "views": 50000, "created_at": "Sat Nov 08 10:00:00 +0000 2026"}},
    "hn.algolia.com/api/v1/items": {"title": "Show HN: My second brain", "points": 321, "author": "pg", "url": "https://sb.dev", "created_at": "2026-10-01", "children": [{"author": "c1", "text": "<p>Great work</p>", "children": [{"author": "c2", "text": "Agreed", "children": []}]}]},
    "www.reddit.com/r/": [{"data": {"children": [{"data": {"title": "Thread", "selftext": "Body", "permalink": "/r/a/comments/xyz/t/", "author": "op", "score": 10, "num_comments": 2, "subreddit": "a", "is_self": True, "created_utc": 1762000000}}]}},
                          {"data": {"children": [{"data": {"author": "c", "score": 5, "body": "Top comment here"}}]}}],
}


def fake_get_json(url):
    for k, v in JSONS.items():
        if k in url:
            return v
    raise R.FetchError(f"no fixture for {url}")


def fake_http_get(url, accept="*/*", check=True):
    if "duckduckgo" in url:
        return DDG, url
    if "news.google.com" in url:
        return NEWS, url
    if "arxiv" in url:
        return ARXIV, url
    if "youtube.com/watch" in url:
        player = {"videoDetails": {"title": "How I take notes", "author": "NoteTube", "shortDescription": "My system", "viewCount": "1000", "lengthSeconds": "600"},
                  "captions": {"playerCaptionsTracklistRenderer": {"captionTracks": [{"baseUrl": "https://www.youtube.com/api/timedtext?v=x", "languageCode": "en"}]}}}
        return f"<script>var ytInitialPlayerResponse = {json.dumps(player)};var meta = 1;</script>", url
    if "timedtext" in url:
        return '<transcript><text start="0">Hello &amp;amp; welcome</text><text start="2">to my notes system</text></transcript>', url
    if "example.com/article" in url:
        return ("<html><head><title>Great Article</title><meta name='description' content='About memory'></head><body>"
                "<nav>Home | About</nav><main><h1>Memory systems</h1><p>" + "Long-term memory matters for AI agents. " * 20 +
                "</p><script>evil()</script></main><footer>(c) 2026</footer></body></html>"), url
    return json.dumps(fake_get_json(url)), url


class ResearchTest(unittest.TestCase):
    def setUp(self):
        self.p1 = mock.patch.object(R, "get_json", side_effect=fake_get_json)
        self.p2 = mock.patch.object(R, "http_get", side_effect=fake_http_get)
        self.p1.start()
        self.p2.start()

    def tearDown(self):
        self.p1.stop()
        self.p2.stop()

    def test_every_search_source(self):
        d = R.search("second brain", [s for s in R.ALL_SOURCES if s not in ("x", "youtube", "github")], 5)
        by = {}
        for r in d["results"]:
            by.setdefault(r["source"], r)  # first result per source
        self.assertEqual(d["errors"], {})
        self.assertEqual(by["web"]["url"], "https://example.com/second-brain")
        self.assertEqual(by["web"]["title"], "The Second Brain guide")
        self.assertEqual(len([r for r in d["results"] if r["source"] == "web"]), 2)  # ad removed
        self.assertEqual(by["news"]["author"], "Tech Daily")
        self.assertEqual(by["wikipedia"]["url"], "https://en.wikipedia.org/wiki/Personal_knowledge_management")
        self.assertEqual(by["hackernews"]["score"], 321)
        self.assertEqual(by["reddit"]["url"], "https://www.reddit.com/r/ObsidianMD/comments/abc/x/")
        self.assertEqual(by["stackoverflow"]["title"], "FTS5 & prefix")
        self.assertEqual(by["arxiv"]["title"], "Memory for Agents")
        self.assertEqual(by["bluesky"]["url"], "https://bsky.app/profile/alice.bsky.social/post/3kabc")
        self.assertEqual(by["mastodon"]["author"], "@bob")
        self.assertEqual(by["lemmy"]["score"], 55)

    def test_failing_source_is_reported_not_fatal(self):
        with mock.patch.object(R, "search_reddit", side_effect=R.FetchError("www.reddit.com answered 429 Too Many Requests")):
            d = R.search("x", ["wikipedia", "reddit"])
        self.assertIn("429", d["errors"]["reddit"])
        self.assertTrue(d["results"])

    def test_read_x_post(self):
        p = R.fetch("https://x.com/jane/status/1234567890")
        self.assertEqual(p["kind"], "x")
        self.assertEqual(p["likes"], 1200)
        self.assertIn("AI memory feature", p["text"])
        self.assertEqual(p["url"], "https://x.com/jane/status/1234567890")

    def test_read_youtube_transcript(self):
        p = R.fetch("https://youtu.be/dQw4w9WgXcQ")
        self.assertEqual(p["title"], "How I take notes")
        self.assertTrue(p["has_transcript"])
        self.assertIn("Hello & welcome to my notes system", p["text"])

    def test_read_threads(self):
        hn = R.fetch("https://news.ycombinator.com/item?id=42")
        self.assertIn("**c1**: Great work", hn["text"])
        self.assertIn("  - **c2**: Agreed", hn["text"])
        rd = R.fetch("https://www.reddit.com/r/a/comments/xyz/t/")
        self.assertIn("Top comment here", rd["text"])

    def test_read_article_is_clean(self):
        p = R.fetch("https://example.com/article")
        self.assertEqual(p["title"], "Great Article")
        self.assertIn("# Memory systems", p["text"])
        self.assertNotIn("evil()", p["text"])
        self.assertNotIn("Home | About", p["text"])
        self.assertEqual(p["description"], "About memory")

    def test_search_x_via_web(self):
        global DDG
        old = DDG
        DDG = '<a class="result__a" href="https://x.com/jane/status/1234567890">Jane on X</a><a class="result__snippet">s</a>'
        try:
            res = R.search_x("ai memory", 3)
        finally:
            DDG = old
        self.assertEqual(res[0].source, "x")
        self.assertEqual(res[0].score, 1200)

    def test_markdown_for_saving(self):
        d = R.search("second brain", ["hackernews", "wikipedia"])
        md = R.results_markdown("second brain", d)
        self.assertIn("# Research: second brain", md)
        self.assertIn("[Show HN: My second brain](https://sb.dev)", md)


class SafetyTest(unittest.TestCase):
    def test_private_addresses_refused(self):
        for url in ("http://127.0.0.1:7777/api/note", "http://localhost/", "http://10.0.0.5/", "http://192.168.1.1/",
                    "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "ftp://example.com/x"):
            with self.assertRaises(R.FetchError, msg=url):
                R._check_public(url)


if __name__ == "__main__":
    unittest.main()
