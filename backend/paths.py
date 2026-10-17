"""Repository paths, independent of the process working directory."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / 'frontend'
DATABASE = ROOT / 'database'
