"""Speed check: build a synthetic vault and time the operations COre does most.

    python scripts/bench.py [notes]        (default 10000)

Targets (warm index, uncached queries): recall < 50 ms, search < 20 ms, append < 30 ms."""

import os
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CORTEX_SEMANTIC", "off")

WORDS = ("alpha beta gamma delta launch budget design review hosting server client invoice meeting travel "
         "flight hotel recipe garden python sqlite index search memory graph node link project task idea "
         "decision lesson market social video note draft backup cloud sync device phone laptop").split()


def make_vault(root: Path, n: int, seed: int = 7) -> None:
    rnd = random.Random(seed)
    folders = ["Projects", "Areas", "Resources", "Daily", "People", "Inbox"]
    for i in range(n):
        f = root / folders[i % len(folders)]
        f.mkdir(parents=True, exist_ok=True)
        links = " ".join(f"[[Note {rnd.randrange(n)}]]" for _ in range(rnd.randint(0, 4)))
        paras = "\n\n".join(" ".join(rnd.choice(WORDS) for _ in range(rnd.randint(20, 60))) for _ in range(rnd.randint(2, 6)))
        (f / f"Note {i}.md").write_text(f"# Note {i}\n\n## {rnd.choice(WORDS).title()}\n{paras}\n\n{links}\n", encoding="utf-8")


def timed(fn, reps):
    out = []
    for i in range(reps):
        t = time.perf_counter()
        fn(i)
        out.append((time.perf_counter() - t) * 1000)
    return statistics.median(out), max(out)


def run(n: int = 10000, reps: int = 20) -> dict:
    from cortex.core import Brain
    with tempfile.TemporaryDirectory() as t:
        os.environ["CORTEX_LOCAL_DIR"] = str(Path(t) / "local")
        v = Path(t) / "vault"
        make_vault(v, n)
        b = Brain(v)
        t0 = time.perf_counter()
        b.sync(force=True)
        build = time.perf_counter() - t0
        rnd = random.Random(1)
        qs = [f"{rnd.choice(WORDS)} {rnd.choice(WORDS)}" for _ in range(reps)]
        b._sync_every = 3600  # measure the query path, not the periodic disk walk
        res = {
            "notes": n,
            "index_build_s": round(build, 2),
            "search_ms": timed(lambda i: b.search(qs[i], 8), reps),
            "recall_ms": timed(lambda i: b.recall(qs[i] + f" {i}", 1200), reps),
            "recall_cached_ms": timed(lambda i: b.recall(qs[0] + " 0", 1200), reps),
            "append_ms": timed(lambda i: b.write(f"Inbox/Note {i}.md", f"- line {i}", agent="bench", mode="append", log=False), reps),
        }
        b.close()
        return res


if __name__ == "__main__":
    r = run(int(sys.argv[1]) if len(sys.argv) > 1 else 10000)
    for k, v in r.items():
        print(f"{k:18} {v if not isinstance(v, tuple) else f'median {v[0]:.1f} ms, max {v[1]:.1f} ms'}")
