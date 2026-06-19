#!/usr/bin/env python3
"""contact_search.py — CLI wrapper that imports from lib."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from contact_search import main

if __name__ == "__main__":
    main()
