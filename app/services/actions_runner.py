from __future__ import annotations

import asyncio
import shlex
import time
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.services.github_client import GitHubClient, GitHubError
from app.services.agent_workflow import AGENT_WORKFLOW_PATH


DANGEROUS_TOKENS = {'rm', 'mkfs', 'dd', 'shutdown', 'reboot', ':(){', 'sudo', 'chmod 777'}

@dataclass
class TerminalRunResult:
    ok: bool
    status: str
    conclusion: str | None
    run_id: int | None
    html_url: str | None
    logs: str


def validate_command(command: str) -> None:
    settings = get_settings()
    if not settings.agent_allow_terminal:
        raise GitHubError('تشغيل الطرفية معطل. فعّل AGENT_ALLOW_TERMINAL=true في Railway.')
    if len(command) > 2000:
        raise GitHubError('الأمر طويل جدًا.')
    lowered = command.lower()
    for token in DANGEROUS_TOKENS:
        if token in lowered:
            raise GitHubError(f'الأمر يحتوي عنصرًا خطيرًا أو غير مسموح: {token}')
    try:
        first = shlex.split(command)[0]
    except Exception:
        raise GitHubError('صيغة الأمر غير صحيحة.')
    if first not in settings.allowed_commands:
        raise GitHubError(f'الأمر {first} غير مسموح. عدّل AGENT_ALLOWED_COMMANDS إذا كنت تريده.')


async def run_agent_command(
    client: GitHubClient,
    owner: str,
    repo: str,
    branch: str,
    command: str,
    workdir: str = '.',
    commit_changes: bool = False,
    progress=None,
) -> TerminalRunResult:
    settings = get_settings()
    validate_command(command)

    if progress:
        await progress('🧩 التأكد من وجود Workflow الطرفية...')
    try:
        await client.get_file(owner, repo, AGENT_WORKFLOW_PATH, branch)
    except Exception:
        raise GitHubError(f'Workflow غير مثبت. نفّذ /install_workflow أولًا أو ثبته من البوت.')

    before = time.time() - 15
    workflow_id = settings.agent_workflow_file
    if progress:
        await progress('🚀 تشغيل GitHub Actions workflow_dispatch...')
    await client.workflow_dispatch(owner, repo, workflow_id, branch, {
        'command': command,
        'workdir': workdir or settings.agent_default_workdir,
        'commit_changes': 'true' if commit_changes else 'false',
        'commit_message': 'Agent terminal changes',
    })

    run = None
    deadline = time.time() + settings.agent_max_command_seconds
    while time.time() < deadline:
        runs = await client.list_workflow_runs(owner, repo, workflow_id, branch=branch, per_page=10)
        candidates = [r for r in runs.get('workflow_runs', []) if r.get('event') == 'workflow_dispatch']
        if candidates:
            run = candidates[0]
            break
        await asyncio.sleep(3)
    if not run:
        raise GitHubError('تم إرسال الأمر لكن لم أجد Run في GitHub Actions.')

    run_id = run['id']
    html_url = run.get('html_url')
    if progress:
        await progress(f'⏳ بدأ التنفيذ. Run ID: {run_id}')

    while time.time() < deadline:
        run = await client.get_workflow_run(owner, repo, run_id)
        status = run.get('status')
        conclusion = run.get('conclusion')
        if progress:
            await progress(f'⌛ الحالة: {status} / النتيجة: {conclusion or "لم تنته بعد"}')
        if status == 'completed':
            logs = await client.get_run_logs_text(owner, repo, run_id)
            return TerminalRunResult(
                ok=conclusion == 'success',
                status=status,
                conclusion=conclusion,
                run_id=run_id,
                html_url=html_url,
                logs=logs[-3500:],
            )
        await asyncio.sleep(8)

    raise GitHubError('انتهى وقت انتظار أمر الطرفية قبل اكتماله.')
