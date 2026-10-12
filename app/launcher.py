"""Windowed entry point with a useful log if startup fails."""
import sys
import traceback
from store_diagnostics import record_request_error
from app_paths import configure_runtime_paths
from app_logging import install_exception_hooks

try:
    install_exception_hooks()
    configure_runtime_paths()
    if len(sys.argv) == 3 and sys.argv[1] == '--apply-update':
        from updater import apply_update
        code = apply_update(sys.argv[2])
    else:
        from main import main
        code = main()
except Exception as exc:
    record_request_error(exc, '启动应用', level='CRITICAL')
    if sys.stderr is not None:
        traceback.print_exc()
    code = 1
sys.exit(code)
