"""``python -m todo_db.mcp`` -> ``server.main()``."""

from __future__ import annotations

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
