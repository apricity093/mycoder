"""Narrow context passed from runtime into tool functions."""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class ToolContext:
    root: Path
    path_resolver: Callable[[str], Path]
    shell_env_provider: Callable[[], dict]
    depth: int
    max_depth: int
    spawn_delegate: Callable[[dict], str]
    spill_reader: Callable[[str, int], str] | None = None

    def path(self, raw_path):
        return self.path_resolver(str(raw_path))

    def shell_env(self):
        return self.shell_env_provider()

    def read_spill(self, spill_id, page=1):
        if self.spill_reader is None:
            raise ValueError("spill retrieval is not enabled")
        return self.spill_reader(str(spill_id), int(page))
