"""Resource roots independent of the application's package directory layout."""
from pathlib import Path
import sys

SOURCE_APP_DIR = Path(__file__).resolve().parents[2]
RESOURCE_ROOT = Path(getattr(sys, '_MEIPASS', SOURCE_APP_DIR))
