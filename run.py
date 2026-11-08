#!/usr/bin/env python3
"""Cortex launcher. Usage: python run.py [init|app|connect|mcp|index]"""
import sys
from pathlib import Path

if sys.version_info < (3, 10):
    sys.exit("Cortex needs Python 3.10 or newer.")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cortex.cli import main  # noqa: E402

if __name__ == "__main__":
    interactive_exe = getattr(sys, "frozen", False) and "mcp" not in sys.argv[1:2] and sys.stdin and sys.stdin.isatty()
    try:
        main()
    except KeyboardInterrupt:
        pass
    except BaseException as e:  # Cortex.exe opened by double-click: keep the window open to show why
        if not interactive_exe or (isinstance(e, SystemExit) and e.code in (0, None)):
            raise
        import traceback
        if not isinstance(e, SystemExit):
            traceback.print_exc()
        else:
            print(e.code)
        input("\nCortex couldn't start (details above). Press Enter to close this window.")
