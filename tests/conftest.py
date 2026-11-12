import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sigil import config as config_mod  # noqa: E402


@pytest.fixture()
def cfg(tmp_path):
    return config_mod.load(tmp_path / "vault", create=True)
