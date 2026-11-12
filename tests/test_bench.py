"""Speed regression guard. Uses a smaller vault than scripts/bench.py and generous limits (CI
machines, Windows and antivirus are slower); the real targets are in scripts/bench.py."""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
os.environ["CORTEX_SEMANTIC"] = "off"

import bench  # noqa: E402


class SpeedTest(unittest.TestCase):
    def test_hot_paths_stay_fast(self):
        n = int(os.environ.get("CORTEX_BENCH_NOTES", "3000"))
        local = os.environ.get("CORTEX_LOCAL_DIR")
        try:
            r = bench.run(n, reps=12)
        finally:
            if local:
                os.environ["CORTEX_LOCAL_DIR"] = local
        print("\n" + ", ".join(f"{k}={v}" for k, v in r.items()))
        self.assertLess(r["search_ms"][0], 80)
        self.assertLess(r["recall_ms"][0], 200)
        self.assertLess(r["append_ms"][0], 120)


if __name__ == "__main__":
    unittest.main()
