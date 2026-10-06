"""Windowed entry point with a useful log if startup fails."""
import os
import sys
import traceback
from pathlib import Path

try:
    from main import main
    code = main()
except Exception:
    details = traceback.format_exc()
    base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
    log = base / 'LCSC3D-error.log'
    try:
        log.write_text(details, encoding='utf-8')
    except OSError:
        log = Path(os.environ.get('TEMP', str(base))) / 'LCSC3D-error.log'
        log.write_text(details, encoding='utf-8')
    if sys.stderr is not None:
        sys.stderr.write(details)
    code = 1
sys.exit(code)
