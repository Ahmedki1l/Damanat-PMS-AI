# Vercel entry point — @vercel/python looks for a module-level `app` ASGI callable.
import sys
import os
import glob

# Vercel prepends its own /var/task/_vendor to sys.path, which contains a stripped-down
# SQLAlchemy that only supports PostgreSQL/pyodbc. We must ensure our installed packages
# (pymysql-aware SQLAlchemy) come FIRST so our create_engine() call works correctly.
_venv_site_pkgs = glob.glob("/var/task/.vercel/python/.venv/lib/python*/site-packages")
for _p in sorted(_venv_site_pkgs, reverse=True):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Also ensure the project root is on the path so `app/` is importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: F401
