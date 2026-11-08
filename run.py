#!/usr/bin/env python3
"""Cortex launcher. Usage: python run.py [init|app|connect|mcp|index]"""
import sys
from pathlib import Path

if sys.version_info < (3, 10):
    sys.exit("Cortex needs Python 3.10 or newer.")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cortex.cli import main  # noqa: E402

main()
