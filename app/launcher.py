"""Windowed entry point with a useful log if startup fails."""
import sys
import traceback
from lcsc3d.services.store_diagnostics import record_request_error
from lcsc3d.core.app_paths import configure_runtime_paths
from lcsc3d.core.app_logging import install_exception_hooks

try:
    install_exception_hooks()
    configure_runtime_paths()
    if len(sys.argv) == 3 and sys.argv[1] == '--apply-update':
        from lcsc3d.services.updater import apply_update
        code = apply_update(sys.argv[2])
    else:
        from lcsc3d.ui.main import main
        code = main()
except Exception as exc:
    record_request_error(exc, '启动应用', level='CRITICAL')
    if sys.stderr is not None:
        traceback.print_exc()
    code = 1
sys.exit(code)
