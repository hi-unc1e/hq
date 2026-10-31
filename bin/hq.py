"""Direct Python entry point for hooks, independent of the client's PATH/cwd."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hqlib.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
