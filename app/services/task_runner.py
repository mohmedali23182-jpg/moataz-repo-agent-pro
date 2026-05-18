from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from app.services.actions_runner import run_agent_command, validate_command
from app.services.github_client import GitHubClient, GitHubError
from app.services.repo_agent import apply_instruction, install_workflow

ProgressFn = Callable[[str], Awaitable[None]] | None


@dataclass
class TaskStepResult:
    step: str
    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskPlanResult:
    ok: bool
    summary: str
    results: list[TaskStepResult]


def split_task_steps(text: str) -> list[str]:
    cleaned = text.strip()
    if not cleaned:
        raise GitHubError('المهمة فارغة.')
    # Accept numbered steps, bullets, or explicit separators.
    lines = [x.rstrip() for x in cleaned.splitlines()]
    chunks: list[str] = []
    current: list[str] = []
    for line in lines:
        marker = re.match(r'^\s*(?:step\s*\d+|\d+\.|[-*]\s+|ثم:?)\s*(.*)$', line, flags=re.I)
        if marker and current:
            chunks.append('\n'.join(current).strip())
            current = [marker.group(1).strip()]
        else:
            current.append(marker.group(1).strip() if marker else line)
    if current:
        chunks.append('\n'.join(current).strip())
    chunks = [c for c in chunks if c]
    return chunks or [cleaned]


def classify_step(step: str) -> tuple[str, str]:
    low = step.strip().lower()
    if low.startswith(('run ', 'terminal ', 'cmd ', 'shell ', 'نفذ ', 'شغل ')):
        return 'terminal', re.sub(r'^(run|terminal|cmd|shell|نفذ|شغل)\s+', '', step.strip(), flags=re.I)
    if low.startswith(('install workflow', 'ثبت workflow', 'ثبت الطرفية')):
        return 'install_workflow', ''
    return 'agent', step


async def execute_task_plan(
    client: GitHubClient,
    owner: str,
    repo: str,
    branch: str,
    task: str,
    workdir: str = '.',
    commit_terminal_changes: bool = False,
    progress: ProgressFn = None,
) -> TaskPlanResult:
    steps = split_task_steps(task)
    results: list[TaskStepResult] = []
    if progress:
        await progress(f'🧭 تم تفكيك المهمة إلى {len(steps)} خطوة.')
    for index, step in enumerate(steps, start=1):
        kind, payload = classify_step(step)
        if progress:
            await progress(f'▶️ الخطوة {index}/{len(steps)}: {kind}')
        try:
            if kind == 'install_workflow':
                res = await install_workflow(client, owner, repo, branch)
                results.append(TaskStepResult(step, True, res.message, res.details))
            elif kind == 'terminal':
                validate_command(payload)
                res = await run_agent_command(
                    client=client,
                    owner=owner,
                    repo=repo,
                    branch=branch,
                    command=payload,
                    workdir=workdir,
                    commit_changes=commit_terminal_changes,
                    progress=progress,
                )
                ok = res.ok
                msg = f'Terminal {res.status}/{res.conclusion}. Run: {res.html_url or res.run_id}'
                results.append(TaskStepResult(step, ok, msg, {'logs': res.logs[-1500:]}))
                if not ok:
                    return TaskPlanResult(False, 'توقفت المهمة بسبب فشل أمر طرفية.', results)
            else:
                res = await apply_instruction(client, owner, repo, branch, payload)
                results.append(TaskStepResult(step, True, res.message, res.details))
        except Exception as exc:
            results.append(TaskStepResult(step, False, str(exc), {}))
            return TaskPlanResult(False, f'فشلت المهمة عند الخطوة {index}: {exc}', results)
    return TaskPlanResult(True, 'اكتملت المهمة بنجاح.', results)
