"""Free research and social-media pulls: no API keys, standard library only.

Search sources (all public, keyless):
  web          DuckDuckGo HTML results
  news         Google News RSS
  wikipedia    Wikipedia search API
  hackernews   Hacker News (Algolia API)
  reddit       Reddit public JSON
  github       GitHub search API (60 requests/hour without a token)
  stackoverflow Stack Exchange API (300 requests/day without a key)
  arxiv        arXiv API (papers)
  bluesky      Bluesky public AppView
  mastodon     Mastodon public hashtag timeline (mastodon.social)
  lemmy        Lemmy (lemmy.world) search
  x            X/Twitter: there is no free search API, so this searches the web
               for x.com posts and reads each one through free embed endpoints
  youtube      YouTube search page (best effort)

Reading a link (fetch): any web page as clean text, plus special handling for
YouTube (title + full transcript), X posts, Reddit threads with top comments,
Hacker News threads, Bluesky posts and GitHub repos (README).

Be a good citizen: these are free services. Cortex sends a plain User-Agent,
caps response sizes, and never fetches private-network addresses (so a
malicious web page can't trick an AI into reading your router or intranet).
"""

from __future__ import annotations

import html
import http.client
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser

UA = "Mozilla/5.0 (compatible; Cortex/1.3; personal research tool)"
MAX_BYTES = 4_000_000
TIMEOUT = 15
ALL_SOURCES = ("web", "news", "wikipedia", "hackernews", "reddit", "github", "stackoverflow",
               "arxiv", "bluesky", "mastodon", "lemmy", "x", "youtube")
DEFAULT_SOURCES = ("web", "news", "wikipedia", "hackernews", "reddit")
SOCIAL_SOURCES = ("x", "reddit", "hackernews", "bluesky", "mastodon", "lemmy", "youtube", "news")


SCORE_LABEL = {"github": "stars", "hackernews": "points", "reddit": "upvotes", "stackoverflow": "votes", "lemmy": "votes",
               "x": "likes", "bluesky": "likes", "mastodon": "likes"}


class FetchError(Exception):
    pass


@dataclass
class Result:
    source: str
    title: str
    url: str
    snippet: str = ""
    date: str = ""
    author: str = ""
    score: int | None = None  # points / likes / stars where the source has them
    extra: dict = field(default_factory=dict)


# ── HTTP ────────────────────────────────────────────────────────────────
def _ip_allowed(raw: str) -> bool:
    ip = ipaddress.ip_address(raw.split("%")[0])
    if ip.version == 6 and ip.ipv4_mapped:  # ::ffff:127.0.0.1 is 127.0.0.1
        ip = ip.ipv4_mapped
    if ip.version == 6 and ip in ipaddress.ip_network("64:ff9b::/96"):  # NAT64 wrapping of IPv4
        ip = ipaddress.ip_address(int(ip) & 0xFFFFFFFF)
    # is_global is False for loopback, private, link-local (cloud metadata), CGNAT 100.64/10,
    # benchmarking, documentation and reserved ranges.
    return ip.is_global and not ip.is_multicast


def _vetted_ip(host: str, port: int | None = None) -> str:
    try:
        infos = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise FetchError(f"can't resolve {host}")
    ips = [i[4][0] for i in infos]
    if not ips or not all(_ip_allowed(ip) for ip in ips):
        raise FetchError(f"{host} is a private/local address; refusing to fetch it")
    return ips[0]


def _check_public(url: str) -> None:
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise FetchError("only http(s) links can be fetched")
    if u.username or u.password:
        raise FetchError("links with embedded passwords are refused")
    _vetted_ip(u.hostname, u.port)


class _GuardedMixin:
    """Resolve, vet and connect to the SAME address in one step. Checking a name and then
    letting the library resolve it again leaves a gap a rebinding DNS server can use."""

    def connect(self):
        if getattr(self, "_tunnel_host", None):  # via a proxy you configured: vet the target, then use the proxy
            _vetted_ip(self._tunnel_host, self._tunnel_port)
            return super().connect()
        ip = _vetted_ip(self.host, self.port)
        self.sock = socket.create_connection((ip, self.port), self.timeout, self.source_address)
        if isinstance(self, http.client.HTTPSConnection):
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _GuardedHTTP(_GuardedMixin, http.client.HTTPConnection):
    pass


class _GuardedHTTPS(_GuardedMixin, http.client.HTTPSConnection):
    pass


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_GuardedHTTP, req)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_GuardedHTTPS, req, context=self._context)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_public(newurl)  # a public page must not redirect us into the local network
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SafeRedirect, _GuardedHTTPHandler, _GuardedHTTPSHandler)


def http_get(url: str, accept: str = "*/*", check: bool = True) -> tuple[str, str]:
    """GET a URL; returns (text, final_url). Raises FetchError with a readable message."""
    if check:
        _check_public(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept,
                                               "Accept-Encoding": "gzip", "Accept-Language": "en"})
    try:
        with _OPENER.open(req, timeout=TIMEOUT) as r:
            data = r.read(MAX_BYTES + 1)
            if r.headers.get("Content-Encoding") == "gzip":
                d = zlib.decompressobj(16 + zlib.MAX_WBITS)
                data = d.decompress(data, MAX_BYTES + 1)  # capped: a 4 MB "zip bomb" can't become 4 GB
            charset = r.headers.get_content_charset() or "utf-8"
            ctype = r.headers.get_content_type()
            final = r.geturl()
    except urllib.error.HTTPError as e:
        raise FetchError(f"{urllib.parse.urlparse(url).hostname} answered {e.code} {e.reason}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError(f"couldn't reach {urllib.parse.urlparse(url).hostname}: {getattr(e, 'reason', e)}")
    if ctype.startswith(("image/", "video/", "audio/")) or ctype in ("application/zip", "application/octet-stream"):
        raise FetchError(f"{ctype} isn't text")
    return data[:MAX_BYTES].decode(charset, errors="replace"), final


def get_json(url: str):
    text, _ = http_get(url, accept="application/json")
    try:
        return json.loads(text)
    except ValueError:
        raise FetchError(f"{urllib.parse.urlparse(url).hostname} didn't return JSON")


def q(s: str) -> str:
    return urllib.parse.quote_plus(s)


# Zero-width, bidi-override and Unicode "tag" characters are invisible to you but readable
# by an AI: a known way to smuggle instructions into fetched text. Strip them.
INVISIBLE = re.compile("[\u200b\u200c\u200e\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]")


def clean(s: str) -> str:
    return INVISIBLE.sub("", s or "")


def safe_url(u: str) -> str:
    return u if isinstance(u, str) and re.match(r"https?://[^\s<>\"']+$", u) else ""


def strip_tags(s: str) -> str:
    return clean(" ".join(html.unescape(re.sub(r"<[^>]+>", " ", s or "")).split()))


# ── readable text from HTML ─────────────────────────────────────────────
class _Readable(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form", "iframe",
            "button", "select", "template"}
    BLOCK = {"p", "div", "section", "article", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
             "pre", "blockquote", "table", "ul", "ol", "dd", "dt", "figcaption", "main"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.title, self.in_title, self.meta = [], 0, "", False, {}
        self.in_main = 0
        self.main_out = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "title":
            self.in_title = True
        elif tag == "meta":
            k = (a.get("property") or a.get("name") or "").lower()
            if k in ("description", "og:description", "og:title", "article:published_time", "author", "og:site_name"):
                self.meta[k] = a.get("content", "")
        if tag in ("main", "article"):
            self.in_main += 1
        if not self.skip and tag in self.BLOCK:
            prefix = {"h1": "\n# ", "h2": "\n## ", "h3": "\n### ", "li": "\n- "}.get(tag, "\n")
            self._emit(prefix)

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == "title":
            self.in_title = False
        if tag in ("main", "article") and self.in_main:
            self.in_main -= 1
        if not self.skip and tag in self.BLOCK:
            self._emit("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip:
            self._emit(data)

    def _emit(self, s):
        self.out.append(s)
        if self.in_main:
            self.main_out.append(s)


def readable(page: str) -> dict:
    p = _Readable()
    try:
        p.feed(page)
    except Exception:
        pass
    body = "".join(p.main_out) if len("".join(p.main_out).strip()) > 400 else "".join(p.out)
    lines, seen = [], set()
    for line in body.splitlines():
        line = " ".join(line.split())
        if not line or (len(line) < 25 and not line.startswith(("#", "-"))) and line.lower() in seen:
            continue
        if line in seen and len(line) < 120:
            continue  # repeated boilerplate (menus, cookie banners)
        seen.add(line)
        lines.append(line)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return {"title": " ".join(p.title.split()) or p.meta.get("og:title", ""), "text": text,
            "description": p.meta.get("description") or p.meta.get("og:description", ""),
            "published": p.meta.get("article:published_time", ""), "site": p.meta.get("og:site_name", "")}


# ── search sources ──────────────────────────────────────────────────────
def _web_ddg_html(query: str, n: int = 8) -> list[Result]:
    page, _ = http_get("https://html.duckduckgo.com/html/?q=" + q(query))
    out = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]+class="result__a"|$)', page, re.S):
        href = html.unescape(m.group(1))
        if "uddg=" in href:  # DuckDuckGo redirect wrapper
            href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0])
        if "duckduckgo.com/y.js" in href:
            continue  # ads
        snip = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', m.group(3), re.S)
        out.append(Result("web", strip_tags(m.group(2)), href, strip_tags(snip.group(1)) if snip else ""))
        if len(out) >= n:
            break
    return out


def search_news(query: str, n: int = 8) -> list[Result]:
    xml, _ = http_get(f"https://news.google.com/rss/search?q={q(query)}&hl=en-US&gl=US&ceid=US:en")
    root = ET.fromstring(xml)
    out = []
    for it in root.iter("item"):
        src = it.find("source")
        out.append(Result("news", strip_tags(it.findtext("title", "")), it.findtext("link", ""),
                          strip_tags(it.findtext("description", ""))[:300], it.findtext("pubDate", ""),
                          src.text if src is not None else ""))
        if len(out) >= n:
            break
    return out


def search_wikipedia(query: str, n: int = 5) -> list[Result]:
    d = get_json(f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={q(query)}&format=json&srlimit={n}")
    return [Result("wikipedia", r["title"], "https://en.wikipedia.org/wiki/" + urllib.parse.quote(r["title"].replace(" ", "_")),
                   strip_tags(r.get("snippet", "")), r.get("timestamp", "")[:10])
            for r in d.get("query", {}).get("search", [])]


def search_hackernews(query: str, n: int = 8) -> list[Result]:
    d = get_json(f"https://hn.algolia.com/api/v1/search?query={q(query)}&tags=story&hitsPerPage={n}")
    return [Result("hackernews", h.get("title") or "", h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
                   f"{h.get('points', 0)} points · {h.get('num_comments', 0)} comments · discuss: https://news.ycombinator.com/item?id={h['objectID']}",
                   (h.get("created_at") or "")[:10], h.get("author", ""), h.get("points"))
            for h in d.get("hits", [])]


def _reddit_json(query: str, n: int = 8, host: str = "www.reddit.com") -> list[Result]:
    d = get_json(f"https://{host}/search.json?q={q(query)}&limit={n}&sort=relevance&t=year")
    out = []
    for c in d.get("data", {}).get("children", []):
        p = c.get("data", {})
        out.append(Result("reddit", p.get("title", ""), "https://www.reddit.com" + p.get("permalink", ""),
                          (p.get("selftext") or "")[:300] or f"r/{p.get('subreddit')} · {p.get('num_comments', 0)} comments",
                          _ts(p.get("created_utc")), "u/" + p.get("author", ""), p.get("score"),
                          {"subreddit": p.get("subreddit"), "comments": p.get("num_comments")}))
    return out


def search_github(query: str, n: int = 6) -> list[Result]:
    d = get_json(f"https://api.github.com/search/repositories?q={q(query)}&sort=stars&per_page={n}")
    return [Result("github", r["full_name"], r["html_url"], r.get("description") or "",
                   (r.get("pushed_at") or "")[:10], r.get("owner", {}).get("login", ""), r.get("stargazers_count"),
                   {"language": r.get("language"), "license": (r.get("license") or {}).get("spdx_id")})
            for r in d.get("items", [])[:n]]


def search_stackoverflow(query: str, n: int = 6) -> list[Result]:
    d = get_json("https://api.stackexchange.com/2.3/search/advanced?order=desc&sort=relevance&site=stackoverflow"
                 f"&q={q(query)}&pagesize={n}&filter=withbody")
    return [Result("stackoverflow", strip_tags(i.get("title", "")), i.get("link", ""), strip_tags(i.get("body", ""))[:300],
                   _ts(i.get("creation_date")), (i.get("owner") or {}).get("display_name", ""), i.get("score"),
                   {"answered": i.get("is_answered"), "answers": i.get("answer_count")})
            for i in d.get("items", [])]


def search_arxiv(query: str, n: int = 5) -> list[Result]:
    xml, _ = http_get(f"http://export.arxiv.org/api/query?search_query=all:{q(query)}&max_results={n}&sortBy=relevance")
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in ET.fromstring(xml).findall("a:entry", ns):
        authors = ", ".join(a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)[:4])
        out.append(Result("arxiv", " ".join(e.findtext("a:title", "", ns).split()), e.findtext("a:id", "", ns),
                          " ".join(e.findtext("a:summary", "", ns).split())[:400], e.findtext("a:published", "", ns)[:10], authors))
    return out


def search_bluesky(query: str, n: int = 8) -> list[Result]:
    d = get_json(f"https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?q={q(query)}&limit={n}&sort=top")
    out = []
    for p in d.get("posts", []):
        handle = p.get("author", {}).get("handle", "")
        rkey = p.get("uri", "").rsplit("/", 1)[-1]
        rec = p.get("record", {})
        out.append(Result("bluesky", (rec.get("text") or "")[:90], f"https://bsky.app/profile/{handle}/post/{rkey}",
                          rec.get("text", ""), (rec.get("createdAt") or "")[:10], "@" + handle, p.get("likeCount"),
                          {"reposts": p.get("repostCount"), "replies": p.get("replyCount")}))
    return out


def search_mastodon(query: str, n: int = 8) -> list[Result]:
    tag = re.sub(r"\W", "", query.split()[0] if len(query.split()) == 1 else query.title().replace(" ", ""))
    d = get_json(f"https://mastodon.social/api/v1/timelines/tag/{urllib.parse.quote(tag)}?limit={n}")
    return [Result("mastodon", strip_tags(s.get("content", ""))[:90], s.get("url") or s.get("uri", ""),
                   strip_tags(s.get("content", "")), (s.get("created_at") or "")[:10], "@" + s.get("account", {}).get("acct", ""),
                   s.get("favourites_count"), {"reblogs": s.get("reblogs_count")})
            for s in d if isinstance(s, dict)]


def search_lemmy(query: str, n: int = 8) -> list[Result]:
    d = get_json(f"https://lemmy.world/api/v3/search?q={q(query)}&type_=Posts&sort=TopAll&limit={n}")
    out = []
    for pv in d.get("posts", []):
        p, c = pv.get("post", {}), pv.get("counts", {})
        out.append(Result("lemmy", p.get("name", ""), p.get("ap_id") or p.get("url", ""), (p.get("body") or "")[:300],
                          (p.get("published") or "")[:10], pv.get("creator", {}).get("name", ""), c.get("score"),
                          {"community": pv.get("community", {}).get("name"), "comments": c.get("comments")}))
    return out


def search_x(query: str, n: int = 6) -> list[Result]:
    """No free X search exists; find x.com posts via web search, then read each one."""
    hits = search_web(f"{query} site:x.com", n * 2) + search_web(f"{query} site:twitter.com", n)
    out, seen = [], set()
    for h in hits:
        m = X_STATUS.search(h.url)
        if not m or m.group(2) in seen:
            continue
        seen.add(m.group(2))
        try:
            post = read_x(h.url)
            out.append(Result("x", post["title"][:90], post["url"], post["text"], post.get("date", ""),
                              post.get("author", ""), post.get("likes"), {k: post.get(k) for k in ("reposts", "replies", "views")}))
        except FetchError:
            out.append(Result("x", h.title, h.url, h.snippet))
        if len(out) >= n:
            break
    return out


def _yt_page_search(query: str, n: int = 6) -> list[Result]:
    page, _ = http_get("https://www.youtube.com/results?search_query=" + q(query))
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", page, re.S)
    if not m:
        raise FetchError("YouTube changed its page; search unavailable (reading a video link still works)")
    out = []
    for vr in _walk(json.loads(m.group(1)), "videoRenderer"):
        vid = vr.get("videoId")
        title = "".join(r.get("text", "") for r in vr.get("title", {}).get("runs", []))
        if not vid or not title:
            continue
        out.append(Result("youtube", title, f"https://www.youtube.com/watch?v={vid}",
                          "".join(r.get("text", "") for r in (vr.get("detailedMetadataSnippets") or [{}])[0].get("snippetText", {}).get("runs", [])),
                          vr.get("publishedTimeText", {}).get("simpleText", ""),
                          "".join(r.get("text", "") for r in vr.get("ownerText", {}).get("runs", [])),
                          extra={"views": vr.get("viewCountText", {}).get("simpleText"), "length": vr.get("lengthText", {}).get("simpleText")}))
        if len(out) >= n:
            break
    return out


def _searcher(source: str):
    return globals()[f"search_{source}"]  # looked up at call time (easy to extend and to test)


def search(query: str, sources=DEFAULT_SOURCES, per_source: int = 6) -> dict:
    """Search several sources in parallel. Returns {"results": [...], "errors": {source: message}}."""
    sources = [s for s in dict.fromkeys(sources) if s in ALL_SOURCES] or list(DEFAULT_SOURCES)
    results, errors = [], {}

    def one(s):
        try:
            return s, _searcher(s)(query, per_source), None
        except FetchError as e:
            return s, [], str(e)
        except Exception as e:  # a source changed its format: report, don't crash
            return s, [], f"couldn't read {s} results ({type(e).__name__})"

    with ThreadPoolExecutor(max_workers=min(8, len(sources))) as ex:
        for s, res, err in ex.map(one, sources):
            for r in res:
                r.url = safe_url(r.url)
                r.title, r.snippet, r.author = clean(r.title), clean(r.snippet), clean(r.author)
                if r.url:
                    results.append(r)
            if err:
                errors[s] = err
    return {"query": query, "results": [asdict(r) for r in results], "errors": errors}


# ── reading a single link ───────────────────────────────────────────────
X_STATUS = re.compile(r"(?:x|twitter|fxtwitter|vxtwitter)\.com/([A-Za-z0-9_]+)/status(?:es)?/(\d+)")
YT_ID = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})")
REDDIT = re.compile(r"reddit\.com/r/[^/]+/comments/[a-z0-9]+")
HN = re.compile(r"news\.ycombinator\.com/item\?id=(\d+)")
BSKY = re.compile(r"bsky\.app/profile/([^/]+)/post/([a-z0-9]+)")
X_PROFILE = re.compile(r"^https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/?$")
GH_REPO = re.compile(r"^https?://github\.com/([\w.-]+)/([\w.-]+)/?$")


def fetch(url: str, max_chars: int = 20000) -> dict:
    """Read any link as clean text. Returns {kind, title, url, text, author, date, ...}."""
    url = url.strip()
    if not re.match(r"https?://", url):
        url = "https://" + url
    profile = X_PROFILE.match(url.split("?")[0])
    if profile and profile.group(1).lower() not in ("home", "explore", "search", "i", "settings", "notifications", "messages"):
        r = x_user_posts(profile.group(1))
        return {**r, "text": r["text"][:max_chars]}
    for pat, fn in ((YT_ID, read_youtube), (X_STATUS, read_x), (REDDIT, read_reddit), (HN, read_hn),
                    (BSKY, read_bluesky), (GH_REPO, read_github)):
        if pat.search(url):
            r = fn(url)
            break
    else:
        page, final = http_get(url, accept="text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5")
        if "<html" in page[:2000].lower() or "<body" in page[:5000].lower():
            rd = readable(page)
            r = {"kind": "page", "title": rd["title"] or final, "url": final, "text": rd["text"],
                 "description": rd["description"], "date": rd["published"], "site": rd["site"]}
        else:
            r = {"kind": "text", "title": final.rsplit("/", 1)[-1] or final, "url": final, "text": page}
    r["text"], r["title"] = clean(r.get("text", "")), clean(r.get("title", ""))
    r["url"] = safe_url(r.get("url", "")) or url
    if len(r.get("text", "")) > max_chars:
        r["text"] = r["text"][:max_chars] + f"\n\n[cut at {max_chars} characters]"
        r["truncated"] = True
    return r


def _x_fx_then_oembed(url: str) -> dict:
    m = X_STATUS.search(url)
    user, sid = m.group(1), m.group(2)
    canonical = f"https://x.com/{user}/status/{sid}"
    try:  # FxTwitter: free public mirror API with full text + stats
        t = get_json(f"https://api.fxtwitter.com/{user}/status/{sid}").get("tweet") or {}
        if t.get("text") is not None:
            text = t["text"]
            if t.get("quote"):
                text += f"\n\n> Quoting @{t['quote'].get('author', {}).get('screen_name', '')}: {t['quote'].get('text', '')}"
            media = [x.get("url") for x in (t.get("media") or {}).get("all", []) if x.get("url")]
            return {"kind": "x", "title": f"@{t.get('author', {}).get('screen_name', user)}: {text[:80]}", "url": canonical,
                    "text": text + ("\n\nMedia: " + " ".join(media) if media else ""),
                    "author": f"{t.get('author', {}).get('name', '')} (@{t.get('author', {}).get('screen_name', user)})",
                    "date": t.get("created_at", ""), "likes": t.get("likes"), "reposts": t.get("retweets"),
                    "replies": t.get("replies"), "views": t.get("views")}
    except FetchError:
        pass
    # Official oEmbed (free, no key): text only.
    d = get_json("https://publish.twitter.com/oembed?omit_script=1&url=" + q(canonical))
    text = strip_tags(re.sub(r"<a[^>]*>(pic\.twitter\.com[^<]*)</a>", r"\1", d.get("html", "").split("&mdash;")[0]))
    return {"kind": "x", "title": f"{d.get('author_name', user)}: {text[:80]}", "url": canonical, "text": text,
            "author": d.get("author_name", user)}


def _yt_page_read(url: str) -> dict:
    vid = YT_ID.search(url).group(1)
    watch = f"https://www.youtube.com/watch?v={vid}"
    page, _ = http_get(watch + "&hl=en")
    m = re.search(r"ytInitialPlayerResponse\s*=\s*(\{.*?\});(?:var|</script>)", page, re.S)
    player = json.loads(m.group(1)) if m else {}
    det = player.get("videoDetails", {})
    title, author = det.get("title", ""), det.get("author", "")
    if not title:
        try:
            o = get_json(f"https://www.youtube.com/oembed?format=json&url={q(watch)}")
            title, author = o.get("title", ""), o.get("author_name", "")
        except FetchError:
            pass
    transcript, lang = "", ""
    tracks = player.get("captions", {}).get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])
    if tracks:
        track = next((t for t in tracks if t.get("languageCode", "").startswith("en") and t.get("kind") != "asr"), None) \
            or next((t for t in tracks if t.get("languageCode", "").startswith("en")), None) or tracks[0]
        lang = track.get("languageCode", "")
        try:
            xml, _ = http_get(track["baseUrl"])
            if xml.strip():
                lines = [html.unescape(strip_tags(n.text or "")) for n in ET.fromstring(xml).iter() if n.tag in ("text", "p")]
                transcript = _paragraphs([l for l in lines if l])
        except (FetchError, ET.ParseError, KeyError):
            transcript = ""
    desc = det.get("shortDescription", "")
    text = (f"## Description\n{desc}\n\n" if desc else "") + (
        f"## Transcript ({lang})\n{transcript}" if transcript else "## Transcript\n(No captions available for this video.)")
    return {"kind": "youtube", "title": title or watch, "url": watch, "text": text, "author": author,
            "views": det.get("viewCount"), "length_seconds": det.get("lengthSeconds"), "has_transcript": bool(transcript)}


def _reddit_thread_json(url: str, host: str = "www.reddit.com") -> dict:
    path = urllib.parse.urlparse(url).path.rstrip("/")
    d = get_json(f"https://{host}{path}.json?limit=40&sort=top")
    post = d[0]["data"]["children"][0]["data"]
    lines = [post.get("selftext", "")]
    if post.get("url") and not post.get("is_self"):
        lines.append(f"Link: {post['url']}")
    lines.append(f"\n## Top comments")
    for c in d[1]["data"]["children"][:25]:
        cd = c.get("data", {})
        if cd.get("body"):
            lines.append(f"- **u/{cd.get('author')}** ({cd.get('score', 0)} points): {' '.join(cd['body'].split())[:800]}")
    return {"kind": "reddit", "title": post.get("title", ""), "url": "https://www.reddit.com" + post.get("permalink", ""),
            "text": "\n".join(lines).strip(), "author": "u/" + post.get("author", ""), "date": _ts(post.get("created_utc")),
            "score": post.get("score"), "comments": post.get("num_comments"), "subreddit": post.get("subreddit")}


def read_hn(url: str) -> dict:
    iid = HN.search(url).group(1)
    d = get_json(f"https://hn.algolia.com/api/v1/items/{iid}")
    lines = [strip_tags(d.get("text") or "")]
    if d.get("url"):
        lines.append(f"Link: {d['url']}")
    lines.append("\n## Top comments")

    def walk(node, depth):
        for ch in (node.get("children") or [])[:12 if depth == 0 else 3]:
            if ch.get("text"):
                lines.append(f"{'  ' * depth}- **{ch.get('author')}**: {strip_tags(ch['text'])[:600]}")
            if depth < 2:
                walk(ch, depth + 1)
    walk(d, 0)
    return {"kind": "hackernews", "title": d.get("title") or "", "url": f"https://news.ycombinator.com/item?id={iid}",
            "text": "\n".join(lines).strip(), "author": d.get("author", ""), "date": (d.get("created_at") or "")[:10],
            "score": d.get("points")}


def read_bluesky(url: str) -> dict:
    m = BSKY.search(url)
    actor, rkey = m.group(1), m.group(2)
    if not actor.startswith("did:"):
        actor = get_json(f"https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle?handle={q(actor)}")["did"]
    d = get_json("https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread?depth=1&uri="
                 + q(f"at://{actor}/app.bsky.feed.post/{rkey}"))
    t = d.get("thread", {})
    p = t.get("post", {})
    lines = [p.get("record", {}).get("text", ""), "\n## Replies"]
    for r in (t.get("replies") or [])[:20]:
        rp = r.get("post", {})
        lines.append(f"- **@{rp.get('author', {}).get('handle')}**: {rp.get('record', {}).get('text', '')}")
    return {"kind": "bluesky", "title": p.get("record", {}).get("text", "")[:80], "url": url, "text": "\n".join(lines),
            "author": "@" + p.get("author", {}).get("handle", ""), "date": (p.get("record", {}).get("createdAt") or "")[:10],
            "likes": p.get("likeCount"), "reposts": p.get("repostCount")}


def read_github(url: str) -> dict:
    m = GH_REPO.match(url.split("?")[0].split("#")[0])
    owner, repo = m.group(1), m.group(2).removesuffix(".git")
    try:
        info = get_json(f"https://api.github.com/repos/{owner}/{repo}")
    except FetchError:  # API rate limit (60/hour without a token): fall back to the raw README
        for name in ("README.md", "readme.md", "README.rst", "README"):
            try:
                readme, _ = http_get(f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{name}")
                return {"kind": "github", "title": f"{owner}/{repo}", "url": f"https://github.com/{owner}/{repo}",
                        "text": "## README\n" + readme, "author": owner}
            except FetchError:
                continue
        raise
    readme = ""
    try:
        r = get_json(f"https://api.github.com/repos/{owner}/{repo}/readme")
        if r.get("download_url"):
            readme, _ = http_get(r["download_url"])
    except FetchError:
        pass
    head = (f"{info.get('description') or ''}\n\nStars: {info.get('stargazers_count')} · Language: {info.get('language')} · "
            f"License: {(info.get('license') or {}).get('spdx_id')} · Last push: {(info.get('pushed_at') or '')[:10]}")
    return {"kind": "github", "title": info.get("full_name", f"{owner}/{repo}"), "url": info.get("html_url", url),
            "text": head + ("\n\n## README\n" + readme if readme else ""), "author": owner,
            "score": info.get("stargazers_count")}


# ── helpers ─────────────────────────────────────────────────────────────
def _ts(v) -> str:
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(float(v), timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def _paragraphs(lines: list[str], size: int = 6) -> str:
    return "\n\n".join(" ".join(lines[i:i + size]) for i in range(0, len(lines), size))


def _walk(obj, key):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            else:
                yield from _walk(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, key)


# ── markdown for saving into the brain ──────────────────────────────────
def results_markdown(query: str, data: dict, pages: list[dict] | None = None) -> str:
    from datetime import date
    lines = ["---", "tags: [research]", "trust: external", f"query: {json.dumps(query)}", f"date: {date.today().isoformat()}",
             f"sources: [{', '.join(sorted({r['source'] for r in data['results']}))}]", "---",
             f"# Research: {query}", ""]
    by_source: dict[str, list] = {}
    for r in data["results"]:
        by_source.setdefault(r["source"], []).append(r)
    n = 0
    for src, items in by_source.items():
        lines.append(f"## {src.title()}")
        for r in items:
            n += 1
            meta = " · ".join(x for x in (r.get("author"), r.get("date"),
                                         f"{r['score']:,} {SCORE_LABEL.get(r['source'], 'points')}" if r.get("score") is not None else "") if x)
            lines.append(f"{n}. [{r['title'] or r['url']}]({r['url']})" + (f" — {meta}" if meta else ""))
            if r.get("snippet"):
                lines.append(f"   > {r['snippet'][:400]}")
        lines.append("")
    for p in pages or []:
        lines += [f"## Read: {p['title']}", f"Source: {p['url']}", "", p["text"][:6000], ""]
    if data.get("errors"):
        lines.append("_Unavailable this time: " + "; ".join(f"{k} ({v})" for k, v in data["errors"].items()) + "_")
    return "\n".join(lines)


def page_markdown(p: dict) -> str:
    from datetime import date
    meta = {k: p[k] for k in ("author", "date", "likes", "reposts", "replies", "views", "score", "comments", "subreddit")
            if p.get(k) not in (None, "")}
    lines = ["---", f"tags: [source, {p['kind']}]", "trust: external", f"url: {json.dumps(p['url'])}", f"saved: {date.today().isoformat()}"]
    lines += [f"{k}: {json.dumps(v) if isinstance(v, str) else v}" for k, v in meta.items()]
    lines += ["---", f"# {p['title'][:150]}", "", f"Source: {p['url']}", ""]
    if p.get("description"):
        lines += [f"> {p['description']}", ""]
    lines.append(p["text"])
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════
# Provider chains: several free backends per source, tried in order.
# A backend that fails (rate limit, block, changed page) is benched for a while
# and skipped; results are cached, so repeat lookups cost nothing and stay polite.
# ════════════════════════════════════════════════════════════════════════
import os as _os
import shutil as _shutil
import subprocess as _subprocess
import tempfile as _tempfile
import threading as _threading
import time as _time
import base64 as _base64

BENCH_SECONDS = 600
SEARCH_TTL = 1800
FETCH_TTL = 6 * 3600
_down: dict[str, float] = {}
_cache: dict[tuple, tuple[float, object]] = {}
_lock = _threading.Lock()

NITTER = ("xcancel.com", "nitter.poast.org", "nitter.privacydev.net", "nitter.net")
INVIDIOUS = ("inv.nadeko.net", "yewtu.be", "invidious.nerdvpn.de", "invidious.privacyredirect.com")
PIPED = ("pipedapi.kavin.rocks", "pipedapi.adminforge.de", "api.piped.private.coffee")


def _cached(key: tuple, ttl: float):
    with _lock:
        hit = _cache.get(key)
        if hit and _time.time() - hit[0] < ttl:
            return hit[1]
    return None


def _store(key: tuple, value) -> None:
    with _lock:
        if len(_cache) > 500:
            _cache.clear()
        _cache[key] = (_time.time(), value)


def chain(name: str, providers, *args, ttl: float = SEARCH_TTL):
    """Try each (label, fn) until one returns something. Returns (value, label_used).
    Failures bench that provider for BENCH_SECONDS so the next call goes straight to a working one."""
    key = (name, args)
    hit = _cached(key, ttl)
    if hit is not None:
        return hit
    errors = []
    for label, fn in providers:
        if _down.get(label, 0) > _time.time():
            errors.append(f"{label} resting")
            continue
        try:
            value = fn(*args)
        except FetchError as e:
            _down[label] = _time.time() + BENCH_SECONDS
            errors.append(f"{label}: {e}")
            continue
        except Exception as e:  # page format changed etc.
            _down[label] = _time.time() + BENCH_SECONDS
            errors.append(f"{label}: unreadable ({type(e).__name__})")
            continue
        if value:
            _store(key, (value, label))
            return value, label
        errors.append(f"{label}: nothing found")
    raise FetchError("; ".join(errors) or "no provider available")


def _tag(results: list[Result], via: str) -> list[Result]:
    for r in results:
        r.extra = {**(r.extra or {}), "via": via}
    return results


# ── web search: DuckDuckGo HTML → DuckDuckGo Lite → Mojeek → Bing ───────
def _web_ddg_lite(query: str, n: int = 8) -> list[Result]:
    page, _ = http_get("https://lite.duckduckgo.com/lite/?q=" + q(query))
    links = re.findall(r"<a[^>]+href=\"([^\"]+)\"[^>]*class=['\"]result-link['\"][^>]*>(.*?)</a>", page, re.S)
    snips = re.findall(r"class=['\"]result-snippet['\"][^>]*>(.*?)</td>", page, re.S)
    out = []
    for i, (href, title) in enumerate(links[:n]):
        href = html.unescape(href)
        if "uddg=" in href:
            href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0])
        out.append(Result("web", strip_tags(title), href, strip_tags(snips[i]) if i < len(snips) else ""))
    return out


def _web_mojeek(query: str, n: int = 8) -> list[Result]:
    page, _ = http_get("https://www.mojeek.com/search?q=" + q(query))
    out = []
    for m in re.finditer(r'<a class="title" href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a class="title"|</ul>)', page, re.S):
        snip = re.search(r'<p class="s">(.*?)</p>', m.group(3), re.S)
        out.append(Result("web", strip_tags(m.group(2)), html.unescape(m.group(1)), strip_tags(snip.group(1)) if snip else ""))
        if len(out) >= n:
            break
    return out


def _bing_target(href: str) -> str:
    href = html.unescape(href)
    if "bing.com/ck/a" in href:  # tracking wrapper: u=a1<base64url(url)>
        u = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("u", [""])[0]
        if u.startswith("a1"):
            try:
                return _base64.urlsafe_b64decode(u[2:] + "=" * (-len(u[2:]) % 4)).decode()
            except (ValueError, UnicodeDecodeError):
                pass
    return href


def _web_bing(query: str, n: int = 8) -> list[Result]:
    page, _ = http_get(f"https://www.bing.com/search?q={q(query)}&setlang=en&count={n}")
    out = []
    for m in re.finditer(r'<li class="b_algo"(.*?)</li>', page, re.S):
        a = re.search(r'<h2[^>]*><a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', m.group(1), re.S)
        if not a:
            continue
        snip = re.search(r"<p[^>]*>(.*?)</p>", m.group(1), re.S)
        out.append(Result("web", strip_tags(a.group(2)), _bing_target(a.group(1)), strip_tags(snip.group(1)) if snip else ""))
        if len(out) >= n:
            break
    return out


def search_web(query: str, n: int = 8) -> list[Result]:
    res, via = chain("web", [("duckduckgo", _web_ddg_html), ("duckduckgo-lite", _web_ddg_lite),
                             ("mojeek", _web_mojeek), ("bing", _web_bing)], query, n)
    return _tag(res, via)


# ── Reddit: JSON (www) → JSON (old) → RSS → PullPush archive → web ──────
def _reddit_rss(query: str, n: int = 8) -> list[Result]:
    xml, _ = http_get(f"https://www.reddit.com/search.rss?q={q(query)}&limit={n}&sort=relevance&t=year")
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in ET.fromstring(xml).findall("a:entry", ns)[:n]:
        link = e.find("a:link", ns)
        cat = e.find("a:category", ns)
        out.append(Result("reddit", e.findtext("a:title", "", ns), link.get("href") if link is not None else "",
                          strip_tags(e.findtext("a:content", "", ns))[:300], (e.findtext("a:updated", "", ns) or "")[:10],
                          (e.findtext("a:author/a:name", "", ns) or ""), None,
                          {"subreddit": cat.get("term") if cat is not None else None}))
    return out


def _reddit_pullpush(query: str, n: int = 8) -> list[Result]:
    d = get_json(f"https://api.pullpush.io/reddit/search/submission/?q={q(query)}&size={n}&sort=desc&sort_type=score")
    return [Result("reddit", p.get("title", ""), "https://www.reddit.com" + p.get("permalink", ""),
                   (p.get("selftext") or "")[:300], _ts(p.get("created_utc")), "u/" + p.get("author", ""), p.get("score"),
                   {"subreddit": p.get("subreddit"), "comments": p.get("num_comments")})
            for p in d.get("data", []) if p.get("permalink")]


def _reddit_via_web(query: str, n: int = 8) -> list[Result]:
    return [Result("reddit", r.title, r.url, r.snippet) for r in search_web(f"{query} site:reddit.com", n)
            if "reddit.com/r/" in r.url]


def search_reddit(query: str, n: int = 8) -> list[Result]:
    res, via = chain("reddit", [("reddit-json", lambda a, b: _reddit_json(a, b)),
                                ("old-reddit-json", lambda a, b: _reddit_json(a, b, "old.reddit.com")),
                                ("reddit-rss", _reddit_rss), ("pullpush", _reddit_pullpush), ("web:reddit", _reddit_via_web)],
                     query, n)
    return _tag(res, via)


def _reddit_thread_rss(url: str) -> dict:
    path = urllib.parse.urlparse(url).path.rstrip("/")
    xml, _ = http_get(f"https://www.reddit.com{path}/.rss?limit=40")
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entries = ET.fromstring(xml).findall("a:entry", ns)
    if not entries:
        raise FetchError("empty thread feed")
    first = entries[0]
    lines = [strip_tags(first.findtext("a:content", "", ns)), "\n## Top comments"]
    for e in entries[1:30]:
        lines.append(f"- **{e.findtext('a:author/a:name', '', ns)}**: {strip_tags(e.findtext('a:content', '', ns))[:800]}")
    return {"kind": "reddit", "title": first.findtext("a:title", "", ns), "url": f"https://www.reddit.com{path}/",
            "text": "\n".join(lines), "author": first.findtext("a:author/a:name", "", ns)}


def _reddit_thread_pullpush(url: str) -> dict:
    m = re.search(r"/comments/([a-z0-9]+)", url)
    pid = m.group(1)
    post = (get_json(f"https://api.pullpush.io/reddit/search/submission/?ids={pid}").get("data") or [None])[0]
    if not post:
        raise FetchError("not in the archive")
    comments = get_json(f"https://api.pullpush.io/reddit/search/comment/?link_id={pid}&size=40&sort=desc&sort_type=score").get("data", [])
    lines = [post.get("selftext", ""), "\n## Top comments (archived)"]
    lines += [f"- **u/{c.get('author')}** ({c.get('score', 0)} points): {' '.join((c.get('body') or '').split())[:800]}" for c in comments]
    return {"kind": "reddit", "title": post.get("title", ""), "url": "https://www.reddit.com" + post.get("permalink", ""),
            "text": "\n".join(lines).strip(), "author": "u/" + post.get("author", ""), "date": _ts(post.get("created_utc")),
            "score": post.get("score"), "comments": post.get("num_comments")}


def read_reddit(url: str) -> dict:
    r, via = chain("reddit-thread", [("reddit-json", _reddit_thread_json),
                                     ("old-reddit-json", lambda u: _reddit_thread_json(u, "old.reddit.com")),
                                     ("reddit-rss", _reddit_thread_rss), ("pullpush", _reddit_thread_pullpush)], url, ttl=FETCH_TTL)
    return {**r, "via": via}


# ── X / Twitter: FxTwitter → VxTwitter → oEmbed; search: Nitter → web ───
def _x_vx(url: str) -> dict:
    m = X_STATUS.search(url)
    d = get_json(f"https://api.vxtwitter.com/{m.group(1)}/status/{m.group(2)}")
    if d.get("text") is None:
        raise FetchError("no post")
    media = " ".join(d.get("mediaURLs") or [])
    return {"kind": "x", "title": f"@{d.get('user_screen_name', m.group(1))}: {d['text'][:80]}",
            "url": f"https://x.com/{m.group(1)}/status/{m.group(2)}", "text": d["text"] + (f"\n\nMedia: {media}" if media else ""),
            "author": f"{d.get('user_name', '')} (@{d.get('user_screen_name', m.group(1))})", "date": d.get("date", ""),
            "likes": d.get("likes"), "reposts": d.get("retweets"), "replies": d.get("replies")}


def read_x(url: str) -> dict:
    r, via = chain("x-post", [("fxtwitter+oembed", _x_fx_then_oembed), ("vxtwitter", _x_vx)], url, ttl=FETCH_TTL)
    return {**r, "via": via}


def _nitter_items(page: str, host: str) -> list[Result]:
    out = []
    for block in re.split(r'<div class="timeline-item', page)[1:]:
        link = re.search(r'<a class="tweet-link" href="/([^/]+)/status/(\d+)', block)
        body = re.search(r'<div class="tweet-content[^"]*"[^>]*>(.*?)</div>', block, re.S)
        if not link or not body:
            continue
        stats = [int(x.replace(",", "")) for x in re.findall(r'<span class="tweet-stat">.*?</span>\s*([\d,]+)', block, re.S)]
        date = re.search(r'<span class="tweet-date"><a[^>]+title="([^"]+)"', block)
        text = strip_tags(body.group(1))
        out.append(Result("x", text[:90], f"https://x.com/{link.group(1)}/status/{link.group(2)}", text,
                          date.group(1) if date else "", "@" + link.group(1), stats[3] if len(stats) > 3 else None,
                          {"replies": stats[0] if stats else None, "reposts": stats[1] if len(stats) > 1 else None}))
    return out


def _x_search_nitter(host: str):
    def run(query: str, n: int = 6) -> list[Result]:
        page, _ = http_get(f"https://{host}/search?f=tweets&q={q(query)}")
        if "timeline-item" not in page:
            raise FetchError(f"{host} returned no timeline (blocked or bot check)")
        return _nitter_items(page, host)[:n]
    return run


def search_x(query: str, n: int = 6) -> list[Result]:
    providers = [(f"nitter:{h}", _x_search_nitter(h)) for h in NITTER] + [("web:x.com", _x_search_via_web)]
    res, via = chain("x", providers, query, n)
    return _tag(res, via)


def _x_search_via_web(query: str, n: int = 6) -> list[Result]:
    """No free X search exists; find x.com posts via web search, then read each one."""
    hits = search_web(f"{query} site:x.com", n * 2)
    out, seen = [], set()
    for h in hits:
        m = X_STATUS.search(h.url)
        if not m or m.group(2) in seen:
            continue
        seen.add(m.group(2))
        try:
            post = read_x(h.url)
            out.append(Result("x", post["title"][:90], post["url"], post["text"], post.get("date", ""), post.get("author", ""),
                              post.get("likes"), {k: post.get(k) for k in ("reposts", "replies", "views")}))
        except FetchError:
            out.append(Result("x", h.title, h.url, h.snippet))
        if len(out) >= n:
            break
    return out


def x_user_posts(handle: str, n: int = 15) -> dict:
    """Recent posts from one account (Nitter RSS mirrors)."""
    handle = re.sub(r"[^A-Za-z0-9_]", "", handle.lstrip("@"))[:15]

    def via(host):
        def run(h):
            xml, _ = http_get(f"https://{host}/{h}/rss")
            items = ET.fromstring(xml).findall(".//item")
            if not items:
                raise FetchError("empty feed")
            lines = []
            for it in items[:n]:
                link = re.sub(r"https?://[^/]+/", "https://x.com/", it.findtext("link", "")).split("#")[0]
                lines.append(f"- {it.findtext('pubDate', '')[:16]} · {strip_tags(it.findtext('description', '') or it.findtext('title', ''))} ({link})")
            return {"kind": "x", "title": f"Recent posts by @{h}", "url": f"https://x.com/{h}", "text": "\n".join(lines), "author": "@" + h}
        return run
    r, used = chain("x-user", [(f"nitter:{h}", via(h)) for h in NITTER], handle, ttl=SEARCH_TTL)
    return {**r, "via": used}


# ── YouTube: page → Invidious → Piped; transcripts also via yt-dlp ───────
def _invidious_search(host: str):
    def run(query: str, n: int = 6) -> list[Result]:
        d = get_json(f"https://{host}/api/v1/search?q={q(query)}&type=video")
        return [Result("youtube", v.get("title", ""), f"https://www.youtube.com/watch?v={v['videoId']}", v.get("description", "")[:300],
                       v.get("publishedText", ""), v.get("author", ""), None,
                       {"views": v.get("viewCount"), "length": v.get("lengthSeconds")})
                for v in d if isinstance(v, dict) and v.get("videoId")][:n]
    return run


def _piped_search(host: str):
    def run(query: str, n: int = 6) -> list[Result]:
        d = get_json(f"https://{host}/search?q={q(query)}&filter=videos")
        out = []
        for v in d.get("items", []):
            vid = re.search(r"v=([A-Za-z0-9_-]{11})", v.get("url", ""))
            if vid:
                out.append(Result("youtube", v.get("title", ""), f"https://www.youtube.com/watch?v={vid.group(1)}",
                                  v.get("shortDescription") or "", v.get("uploadedDate") or "", v.get("uploaderName", ""), None,
                                  {"views": v.get("views"), "length": v.get("duration")}))
        return out[:n]
    return run


def search_youtube(query: str, n: int = 6) -> list[Result]:
    providers = [("youtube", _yt_page_search)] + [(f"invidious:{h}", _invidious_search(h)) for h in INVIDIOUS] \
        + [(f"piped:{h}", _piped_search(h)) for h in PIPED]
    res, via = chain("youtube", providers, query, n)
    return _tag(res, via)


def parse_vtt(vtt: str) -> list[str]:
    """Caption file (WebVTT/SRT/YouTube XML) → plain lines, with auto-caption repeats removed."""
    if vtt.lstrip().startswith("<"):
        try:
            return [html.unescape(strip_tags(n.text or "")) for n in ET.fromstring(vtt).iter() if n.tag in ("text", "p") and n.text]
        except ET.ParseError:
            pass
    lines, last = [], ""
    for line in vtt.splitlines():
        line = line.strip()
        if not line or "-->" in line or line.isdigit() or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        line = strip_tags(re.sub(r"<\d\d:\d\d[^>]*>", "", line))
        if line and line != last and not (last and last.endswith(line)):
            lines.append(line)
            last = line
    return lines


def _transcript_ytdlp(vid: str) -> str:
    exe = _shutil.which("yt-dlp")
    if not exe:
        raise FetchError("yt-dlp not installed")
    with _tempfile.TemporaryDirectory() as d:
        # Fixed argument list (no shell); the URL is rebuilt from a validated 11-character id.
        _subprocess.run([exe, "--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", "en.*,en",
                         "--sub-format", "vtt", "--no-playlist", "--quiet", "--no-warnings", "-o", _os.path.join(d, "%(id)s.%(ext)s"),
                         f"https://www.youtube.com/watch?v={vid}"], capture_output=True, timeout=90, check=False)
        files = sorted(Path(d).glob("*.vtt"), key=lambda f: ("orig" in f.name, f.stat().st_size), reverse=True)
        if not files:
            raise FetchError("yt-dlp found no captions")
        return _paragraphs(parse_vtt(files[0].read_text(encoding="utf-8", errors="replace")))


def _transcript_invidious(host: str):
    def run(vid: str) -> str:
        caps = get_json(f"https://{host}/api/v1/captions/{vid}").get("captions", [])
        cap = next((c for c in caps if c.get("languageCode", "").startswith("en")), caps[0] if caps else None)
        if not cap:
            raise FetchError("no captions")
        vtt, _ = http_get(f"https://{host}{cap['url']}")
        return _paragraphs(parse_vtt(vtt))
    return run


def _transcript_piped(host: str):
    def run(vid: str) -> str:
        subs = get_json(f"https://{host}/streams/{vid}").get("subtitles", [])
        sub = next((s for s in subs if s.get("code", "").startswith("en") and not s.get("autoGenerated")), None) \
            or next((s for s in subs if s.get("code", "").startswith("en")), subs[0] if subs else None)
        if not sub:
            raise FetchError("no subtitles")
        vtt, _ = http_get(sub["url"])
        return _paragraphs(parse_vtt(vtt))
    return run


def read_youtube(url: str) -> dict:
    vid = YT_ID.search(url).group(1)
    hit = _cached(("yt-read", vid), FETCH_TTL)
    if hit:
        return hit[0]
    try:
        base = _yt_page_read(url)
    except FetchError:
        base = {"kind": "youtube", "title": f"https://www.youtube.com/watch?v={vid}", "url": f"https://www.youtube.com/watch?v={vid}",
                "text": "## Transcript\n(No captions available for this video.)", "author": "", "has_transcript": False}
    if not base.get("has_transcript"):
        # YouTube's own caption links increasingly need a browser token; use the alternatives.
        providers = [("yt-dlp", _transcript_ytdlp)] + [(f"invidious:{h}", _transcript_invidious(h)) for h in INVIDIOUS] \
            + [(f"piped:{h}", _transcript_piped(h)) for h in PIPED]
        try:
            transcript, via = chain("yt-transcript", providers, vid, ttl=FETCH_TTL)
            desc = base["text"].split("## Transcript")[0]
            base = {**base, "text": f"{desc}## Transcript (via {via})\n{transcript}", "has_transcript": True, "via": via}
        except FetchError as e:
            base["text"] += f"\n\n(Transcript sources tried: {e})"
    _store(("yt-read", vid), (base, "youtube"))
    return base
