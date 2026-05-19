from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from app.services.actions_runner import run_agent_command
from app.services.github_client import GitHubClient


@dataclass
class SandboxResult:
    ok: bool
    backend: str
    command: str
    logs: str
    run_id: int | None = None


async def sandbox_run_github_actions(
    client: GitHubClient,
    owner: str,
    repo: str,
    branch: str,
    command: str,
    workdir: str = '.',
    progress: Callable[[str], Awaitable[None]] | None = None,
) -> SandboxResult:
    result = await run_agent_command(client, owner, repo, branch, command, workdir, False, progress=progress)
    return SandboxResult(bool(result.ok), 'github_actions', command, result.logs or '', result.run_id)
