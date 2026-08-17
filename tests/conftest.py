"""Shared pytest fixtures.

`isolated_project_env` keeps tests that use pytest's tmp_path from accidentally
discovering the real repository `.env` through an upward directory walk. The
fixture uses the same seam the runtime already provides (`repo_root_override`)
and scopes project-env discovery to the current tmp_path.
"""

from pathlib import Path

import pytest


@pytest.fixture
def isolated_project_env(tmp_path, monkeypatch):
    import apricity.config as config_module
    import apricity.workspace as workspace_module

    boundary = Path(tmp_path).resolve()

    def find_project_env(start):
        current = Path(start).resolve()
        if current.is_file():
            current = current.parent
        while True:
            env_path = current / ".env"
            if env_path.exists():
                return env_path
            if current == boundary or current.parent == current:
                return None
            current = current.parent

    monkeypatch.setattr(config_module, "find_project_env", find_project_env)

    original_build = workspace_module.WorkspaceContext.build

    def build(cwd, repo_root_override=None):
        if repo_root_override is None:
            repo_root_override = cwd
        return original_build(cwd, repo_root_override=repo_root_override)

    monkeypatch.setattr(workspace_module.WorkspaceContext, "build", staticmethod(build))
