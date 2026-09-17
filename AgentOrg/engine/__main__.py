#!/usr/bin/env python3
"""__main__.py — so `python3 -m engine <command>` works.

A thin shim rather than logic: the CLI is importable as a module, and this makes the package
directly runnable without the caller needing to know that.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
