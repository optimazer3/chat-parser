import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("TG_API_ID", "1")
os.environ.setdefault("TG_API_HASH", "x")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("AUTHOR_SALT", "test-salt")
