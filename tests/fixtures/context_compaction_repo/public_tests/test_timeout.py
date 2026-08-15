import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from centralized_timeout_default import DEFAULT_TIMEOUT


def test_timeout_default():
    assert DEFAULT_TIMEOUT == 120
