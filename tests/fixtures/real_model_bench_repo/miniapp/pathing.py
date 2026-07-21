from pathlib import Path


def resolve_workspace_path(root, relative_path):
    return Path(root) / relative_path
