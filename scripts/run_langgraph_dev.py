"""Run LangGraph dev mode without watching generated user workspaces."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import dotenv_values
from langgraph_api.cli import run_server
from langgraph_cli.config import validate_config_file


def main() -> None:
    workspace_root = Path(__file__).resolve().parent.parent
    config = validate_config_file(workspace_root / "langgraph.json")

    # Match LangGraph CLI dependency path setup before importing graph modules.
    sys.path.append(str(workspace_root))
    for dependency in config.get("dependencies", []):
        dependency_path = workspace_root / dependency
        if dependency_path.is_dir():
            sys.path.append(str(dependency_path))

    configured_root = os.environ.get("ARTIFACT_ROOT")
    env_path = config.get("env")
    if not configured_root and isinstance(env_path, (str, Path)):
        configured_root = dotenv_values(workspace_root / env_path).get("ARTIFACT_ROOT")
    artifact_root = Path(configured_root or "data/users").expanduser()
    if not artifact_root.is_absolute():
        artifact_root = workspace_root / artifact_root
    artifact_root = artifact_root.resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)

    # Generated analysis scripts are data, not application source. Excluding the
    # whole workspace root prevents successful runs from breaking their SSE stream.
    run_server(
        host="127.0.0.1",
        port=2024,
        reload=True,
        reload_excludes=[str(artifact_root)],
        graphs=config.get("graphs"),
        n_jobs_per_worker=4,
        open_browser=False,
        env=env_path,
        store=config.get("store"),
        auth=config.get("auth"),
        http=config.get("http"),
        ui=config.get("ui"),
        ui_config=config.get("ui_config"),
        webhooks=config.get("webhooks"),
        checkpointer=config.get("checkpointer"),
        disable_persistence=config.get("disable_persistence", False),
    )


if __name__ == "__main__":
    main()
