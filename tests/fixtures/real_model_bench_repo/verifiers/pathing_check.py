from pathlib import Path

import pytest

from miniapp.pathing import resolve_workspace_path


def test_resolve_workspace_path_accepts_only_paths_inside_root(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    assert resolve_workspace_path(root, "src/app.py") == (root / "src/app.py").resolve()

    with pytest.raises(ValueError):
        resolve_workspace_path(root, "../outside.txt")
    with pytest.raises(ValueError):
        resolve_workspace_path(root, Path(tmp_path.anchor) / "absolute.txt")
