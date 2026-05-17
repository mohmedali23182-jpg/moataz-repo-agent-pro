from __future__ import annotations

import asyncio
import io
import re
import time
import zipfile
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.services.agent_workflow import AGENT_WORKFLOW_CONTENT, AGENT_WORKFLOW_ID, AGENT_WORKFLOW_PATH
from app.services.github_client import GitHubClient, GitHubError


DANGEROUS_PATTERNS = [
    r'\brm\s+-rf\s+/',
    r'\bsudo\b',
    r'\bmkfs\b',
    r'\bdd\s+if=',
    r':\s*\(\s*\)\s*\{\s*:\s*\|\s*:',
    r'\bshutdown\b',
    r'\breboot\b',
    r'\bpasswd\b',
    r'\bchown\s+-R\s+/',
    r'\bchmod\s+-R\s+777\s+/',
]


@dataclass
class AgentCommandResult:
    run_id: int | None
    status: str
    conclusion: str | None
    html_url: str | None
    logs: str


class ActionsRunner:
    def __init__(self, client: GitHubClient) -> None:
        self.client = client
        self.settings = get_settings()

    def _allowed_roots(self) -> set[str]:
        raw = self.settings.agent_allowed_commands.strip()
        if not raw:
            raw = 'npm,pnpm,yarn,python,pip,pytest,node,git,ls,cat,sed,grep'
        return {x.strip() for x in raw.split(',') if x.strip()}

    def validate_command(self, command: str) -> None:
        if not self.settings.agent_allow_terminal:
            raise GitHubError('تشغيل الطرفية معطل. اضبط AGENT_ALLOW_TERMINAL=true في Railway.')
        if not command.strip():
            raise GitHubError('الأمر فارغ.')
        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, command, flags=re.IGNORECASE):
                raise GitHubError(f'تم رفض الأمر لوجود نمط خطير: {pattern}')
        roots = self._allowed_roots()
        first = command.strip().split()[0].strip()
        first = first.split('/')[-1]
        if first not in roots:
            raise GitHubError(
                'الأمر غير مسموح. أول كلمة يجب أن تكون واحدة من: ' + ', '.join(sorted(roots))
            )

    async def ensure_workflow(self, owner: str, repo: str, branch: str) -> bool:
        try:
            content, _ = await self.client.get_file(owner, repo, AGENT_WORKFLOW_PATH, branch)
            return 'workflow_dispatch' in content and 'Agent Command' in content
        except Exception:
            await self.client.put_file(
                owner=owner,
                repo=repo,
                path=AGENT_WORKFLOW_PATH,
                content=AGENT_WORKFLOW_CONTENT,
                branch=branch,
                message='Install Moataz Repo Agent command workflow',
            )
            return False

    async def dispatch_and_wait(
        self,
        owner: str,
        repo: str,
        branch: str,
        command: str,
        workdir: str = '.',
        commit_changes: bool = False,
        commit_message: str = 'Apply agent terminal changes',
    ) -> AgentCommandResult:
        self.validate_command(command)
        existed = await self.ensure_workflow(owner, repo, branch)
        if not existed:
            raise GitHubError(
                'تم تثبيت workflow الطرفية في المستودع. أعد تنفيذ /term بعد 30-60 ثانية حتى يتعرف GitHub Actions عليه.'
            )

        started_at = time.time()
        await self.client.workflow_dispatch(
            owner=owner,
            repo=repo,
            workflow_id=AGENT_WORKFLOW_ID,
            ref=branch,
            inputs={
                'command': command,
                'workdir': workdir or '.',
                'commit_changes': 'true' if commit_changes else 'false',
                'commit_message': commit_message or 'Apply agent terminal changes',
            },
        )

        run = await self._wait_for_new_run(owner, repo, branch, started_at)
        run_id = int(run['id'])
        final = await self._wait_for_run(owner, repo, run_id)
        logs = await self.get_run_logs(owner, repo, run_id)
        return AgentCommandResult(
            run_id=run_id,
            status=final.get('status', ''),
            conclusion=final.get('conclusion'),
            html_url=final.get('html_url'),
            logs=logs,
        )

    async def _wait_for_new_run(self, owner: str, repo: str, branch: str, started_at: float) -> dict[str, Any]:
        for _ in range(30):
            runs = await self.client.list_workflow_runs(owner, repo, AGENT_WORKFLOW_ID, branch=branch, per_page=10)
            for run in runs.get('workflow_runs', []):
                if run.get('event') == 'workflow_dispatch':
                    created_at = run.get('created_at', '')
                    # created_at is trusted enough for ordering; GitHub returns newest first.
                    return run
            await asyncio.sleep(2)
        raise GitHubError('تم إرسال workflow_dispatch لكن لم يظهر run جديد خلال المهلة.')

    async def _wait_for_run(self, owner: str, repo: str, run_id: int) -> dict[str, Any]:
        max_seconds = max(30, int(self.settings.agent_max_command_seconds))
        deadline = time.time() + max_seconds
        last = None
        while time.time() < deadline:
            last = await self.client.get_workflow_run(owner, repo, run_id)
            if last.get('status') == 'completed':
                return last
            await asyncio.sleep(5)
        raise GitHubError(f'انتهت مهلة انتظار GitHub Actions بعد {max_seconds} ثانية. Run ID: {run_id}')

    async def get_run_logs(self, owner: str, repo: str, run_id: int) -> str:
        raw = await self.client.get_workflow_run_logs_zip(owner, repo, run_id)
        if not raw:
            return ''
        parts: list[str] = []
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for name in sorted(zf.namelist()):
                if name.endswith('/'):
                    continue
                try:
                    text = zf.read(name).decode('utf-8', errors='replace')
                except Exception:
                    continue
                parts.append(f'===== {name} =====\n{text}')
        logs = '\n\n'.join(parts)
        if len(logs) > 12000:
            logs = logs[-12000:]
        return logs
