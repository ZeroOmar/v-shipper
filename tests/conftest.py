"""Make the repo root importable as `app` when running `pytest` from anywhere."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
