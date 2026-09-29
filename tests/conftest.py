"""Put the package on the import path so plain `pytest` works.

CI sets PYTHONPATH=src; this keeps a bare local run working too.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
