"""Make the repo root importable so tests can `import app.*` regardless of how
pytest is invoked."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
