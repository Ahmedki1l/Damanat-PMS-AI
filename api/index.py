# Vercel entry point — @vercel/python looks for a module-level `app` ASGI callable.
# sys.path must include the project root so `app/` is importable from within `api/`.
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: F401
