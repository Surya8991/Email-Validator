import sys
from pathlib import Path

# Ensure project root is on sys.path so `app` package is importable
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mangum import Mangum  # noqa: E402

from app.main import app  # noqa: E402, F401

handler = Mangum(app, lifespan="auto")
