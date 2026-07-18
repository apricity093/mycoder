import subprocess
import sys
from pathlib import Path

import mini_apricity


def test_mini_apricity_module_and_public_exports():
    assert mini_apricity.Apricity is not None
    assert mini_apricity.FakeModelClient is not None
    assert not hasattr(mini_apricity, "MiniAgent")
    result = subprocess.run([sys.executable, "-m", "mini_apricity", "--help"], capture_output=True, text=True, check=True)
    assert "Teaching-sized Apricity agent harness" in result.stdout


def test_readme_main_mapping_points_to_existing_files():
    repo_root = Path(__file__).resolve().parents[3]
    main_files = [
        "apricity/cli.py",
        "apricity/runtime.py",
        "apricity/agent_loop.py",
        "apricity/context_manager.py",
        "apricity/providers/clients.py",
        "apricity/tool_executor.py",
        "apricity/tools.py",
        "apricity/task_state.py",
        "apricity/run_store.py",
        "apricity/workspace.py",
    ]
    for path in main_files:
        assert (repo_root / path).exists()
