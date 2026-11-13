"""The vault as a graph: which notes are central, which belong together, which are alone.

Pure Python, no libraries. Exact algorithms up to EXACT_LIMIT notes; above that, closeness and
betweenness are estimated from a fixed sample of starting notes (standard practice, and the
ranking of the top notes is what matters). Results are cached until the vault changes.

Metrics (all scaled 0..1 within the vault):
- degree       how many notes it touches
- pagerank     importance passed along links (the "hub" score; also boosts recall)
- eigenvector  connected to other well-connected notes
- closeness    how few steps it is from everything else
- betweenness  how often it sits on the shortest path between other notes (a bridge)
- cluster      community found by label propagation
"""

from __future__ import annotations

import random
import re
import time
from collections import Counter, deque

EXACT_LIMIT = 1500
SAMPLE = 200
METRICS = ("degree", "pagerank", "eigenvector", "closeness", "betweenness")


def build(brain, conversations: bool = False) -> dict:
    """Nodes and undirected-for-analysis edges from the link table (+ unresolved targets)."""
    brain.sync()
    where = "" if conversations else " WHERE path NOT LIKE 'Conversations/%'"
    nodes = {r["path"]: {"id": r["path"], "title": r["title"], "category": r["category"] or "",
                         "folder": r["path"].split("/", 1)[0] if "/" in r["path"] else "",
                         "tags": r["tags"].split() if r["tags"] else []}
             for r in brain.db.execute(f"SELECT path,title,category,tags FROM notes{where}")}
    edges, unresolved = set(), Counter()
    for r in brain.db.execute("SELECT src,dst FROM links"):
        if r["src"] not in nodes:
            continue
        t = brain._link_target(r["dst"])
        if t and t != r["src"] and t in nodes:
            edges.add((r["src"], t))
        elif not t:
            name = r["dst"].strip()
            if name and not re.match(r"^https?:", name):
                unresolved[name] += 1
                edges.add((r["src"], "?" + name))
    for name in unresolved:
        nodes["?" + name] = {"id": "?" + name, "title": name, "category": "", "folder": "", "tags": [], "missing": True}
    return {"nodes": nodes, "edges": sorted(edges)}


def _adjacency(nodes, edges):
    und = {n: set() for n in nodes}
    out = {n: set() for n in nodes}
    for a, b in edges:
        und[a].add(b)
        und[b].add(a)
        out[a].add(b)
    return und, out


def pagerank(nodes, out, d=0.85, iters=40):
    n = len(nodes) or 1
    pr = {k: 1 / n for k in nodes}
    inbound = {k: [] for k in nodes}
    for a, targets in out.items():
        for b in targets:
            inbound[b].append(a)
    outdeg = {k: len(v) for k, v in out.items()}
    for _ in range(iters):
        sink = sum(pr[k] for k in nodes if not outdeg[k]) / n
        new = {k: (1 - d) / n + d * (sink + sum(pr[j] / outdeg[j] for j in inbound[k])) for k in nodes}
        delta = sum(abs(new[k] - pr[k]) for k in nodes)
        pr = new
        if delta < 1e-7:
            break
    return pr


def eigenvector(und, iters=30):
    x = {k: 1.0 for k in und}
    for _ in range(iters):
        y = {k: x[k] + sum(x[j] for j in und[k]) for k in und}  # +x: shifted so it converges on bipartite graphs
        norm = max(y.values() or [1]) or 1
        y = {k: v / norm for k, v in y.items()}
        if sum(abs(y[k] - x[k]) for k in und) < 1e-6:
            x = y
            break
        x = y
    return x


def closeness_betweenness(und, sources):
    """Harmonic closeness and Brandes betweenness from the given source nodes (integer-indexed
    BFS: about 3x faster than dict-based in pure Python)."""
    keys = list(und)
    idx = {k: i for i, k in enumerate(keys)}
    adj = [[idx[w] for w in und[k]] for k in keys]
    n = len(keys)
    close = [0.0] * n
    btw = [0.0] * n
    for src in sources:
        s = idx[src]
        dist = [-1] * n
        sigma = [0] * n
        dist[s], sigma[s] = 0, 1
        order, preds = [s], {s: []}
        head = 0
        while head < len(order):
            v = order[head]
            head += 1
            dv = dist[v] + 1
            for w in adj[v]:
                if dist[w] < 0:
                    dist[w] = dv
                    preds[w] = [v]
                    sigma[w] = sigma[v]
                    order.append(w)
                elif dist[w] == dv:
                    sigma[w] += sigma[v]
                    preds[w].append(v)
        delta = [0.0] * n
        for w in reversed(order):
            if w != s:
                close[w] += 1 / dist[w]  # harmonic: works on disconnected graphs
                coeff = (1 + delta[w]) / sigma[w]
                for v in preds[w]:
                    delta[v] += sigma[v] * coeff
                btw[w] += delta[w]
    return dict(zip(keys, close)), dict(zip(keys, btw))


def clusters(und, iters=20):
    """Label propagation, deterministic (sorted order, ties to the smallest label)."""
    label = {k: i for i, k in enumerate(sorted(und))}
    order = sorted(und, key=lambda k: (-len(und[k]), k))
    for _ in range(iters):
        changed = 0
        for v in order:
            if not und[v]:
                continue
            counts = Counter(label[w] for w in und[v])
            top = max(counts.values())
            best = min(l for l, c in counts.items() if c == top)
            if best != label[v]:
                label[v] = best
                changed += 1
        if not changed:
            break
    renum = {l: i for i, (l, _) in enumerate(Counter(label.values()).most_common())}
    return {k: renum[l] for k, l in label.items()}


def _scale(m: dict) -> dict:
    hi = max(m.values() or [0]) or 1
    return {k: round(v / hi, 4) for k, v in m.items()}


def analyze(brain, conversations: bool = False, max_age: float = 0.0) -> dict:
    """Graph + every metric, cached on the brain until the index changes (or for max_age seconds)."""
    cache = getattr(brain, "_graph_cache", None)
    key = (brain._gen, conversations)
    if cache and (cache["key"] == key or (max_age and time.monotonic() - cache["at"] < max_age and cache["key"][1] == conversations)):
        return cache["data"]
    g = build(brain, conversations)
    nodes, edges = g["nodes"], g["edges"]
    und, out = _adjacency(nodes, edges)
    real = [k for k in nodes if not nodes[k].get("missing")]
    exact = len(nodes) <= EXACT_LIMIT
    # Above the limit, estimate from a sample sized to a fixed amount of work (~3M steps).
    k = len(nodes) if exact else min(len(nodes), max(32, min(SAMPLE, int(3e6 / (len(nodes) + 2 * len(edges) + 1)))))
    sources = list(nodes) if exact else random.Random(42).sample(sorted(nodes), k)
    close, btw = closeness_betweenness(und, sources)
    scale = 1.0 if exact else len(nodes) / k
    metrics = {
        "degree": {k: len(und[k]) for k in nodes},
        "pagerank": pagerank(nodes, out),
        "eigenvector": eigenvector(und),
        "closeness": {k: v * scale for k, v in close.items()},
        "betweenness": {k: v * scale for k, v in btw.items()},
    }
    scaled = {m: _scale(v) for m, v in metrics.items()}
    comm = clusters(und)
    for k, node in nodes.items():
        node["degree"] = metrics["degree"][k]
        for m in METRICS[1:]:
            node[m] = scaled[m][k]
        node["cluster"] = comm[k]
    sizes = Counter(comm[k] for k in real)
    cluster_info = []
    for cid, size in sizes.most_common(30):
        members = [k for k in real if comm[k] == cid]
        if size < 2:
            continue
        top = sorted(members, key=lambda k: -nodes[k]["pagerank"])[:5]
        cats = Counter(nodes[k]["category"] for k in members if nodes[k]["category"])
        words = Counter(w.lower() for k in members for w in re.findall(r"[A-Za-z]{4,}", nodes[k]["title"]))
        cluster_info.append({"id": cid, "size": size, "top": top, "category": cats.most_common(1)[0][0] if cats else "",
                             "keywords": [w for w, _ in words.most_common(5)]})
    data = {"nodes": nodes, "edges": edges, "clusters": cluster_info, "exact": exact,
            "orphans": sorted(k for k in real if not und[k] and not k.startswith("Daily/")),
            "missing": sorted(k[1:] for k in nodes if nodes[k].get("missing")),
            "stats": {"notes": len(real), "links": len(edges), "clusters": len(cluster_info)}}
    brain._graph_cache = {"key": key, "at": time.monotonic(), "data": data}
    return data


def view(brain, focus: str | None = None, depth: int = 1, metric: str = "pagerank", limit: int = 60,
         conversations: bool = False) -> dict:
    """What a client shows or reasons over: the top notes by a metric, or the neighbourhood of one note."""
    if metric not in METRICS:
        raise ValueError(f"metric must be one of: {', '.join(METRICS)}")
    data = analyze(brain, conversations)
    nodes = data["nodes"]
    limit = max(1, min(int(limit), 2000))
    if focus:
        start = brain.find(focus)
        if not start or start not in nodes:
            raise FileNotFoundError(f"no note named {focus!r}")
        und, _ = _adjacency(nodes, data["edges"])
        keep, frontier = {start}, {start}
        for _ in range(max(1, min(int(depth), 3))):
            frontier = {w for v in frontier for w in und[v]} - keep
            keep |= frontier
        chosen = sorted(keep, key=lambda k: (k != start, -nodes[k].get(metric, 0)))[:limit]
    else:
        chosen = sorted((k for k in nodes if not nodes[k].get("missing")), key=lambda k: -nodes[k].get(metric, 0))[:limit]
    ids = set(chosen)
    if not focus:  # unresolved link targets touching what's shown (drawn as red dots, like Obsidian)
        extra = [b for a, b in data["edges"] if a in ids and b.startswith("?")]
        chosen += sorted(set(extra))[:300]
        ids = set(chosen)
    return {"metric": metric, "focus": focus, "nodes": [nodes[k] for k in chosen],
            "edges": [{"source": a, "target": b} for a, b in data["edges"] if a in ids and b in ids],
            "clusters": data["clusters"][:12], "stats": data["stats"], "exact": data["exact"],
            "orphans": data["orphans"][:50], "missing": data["missing"][:50]}


def hub_boost(brain) -> dict[str, float]:
    """PageRank per note (0..1) for recall's ranking. Cheap (no path metrics) and recomputed at most
    once a minute, so a busy write stream never makes recall wait."""
    c = getattr(brain, "_hub_cache", None)
    if c and (c[0] == brain._gen or time.monotonic() - c[1] < 60):
        return c[2]
    g = build(brain, False)
    _, out = _adjacency(g["nodes"], g["edges"])
    pr = _scale(pagerank(g["nodes"], out, iters=25))
    boost = {k: v for k, v in pr.items() if not k.startswith("?")}
    brain._hub_cache = (brain._gen, time.monotonic(), boost)
    return boost
