from __future__ import annotations

import json
from typing import Awaitable, Callable, Any

from app.agent.memory import index_repository_memory
from app.agent.planner import AgentPlan, AgentStep
from app.services.actions_runner import run_agent_command
from app.services.github_client import GitHubClient, GitHubError
from app.services.repo_agent import analyze_repository, apply_instruction, install_workflow
from app.services.store import Store

Progress = Callable[[str], Awaitable[None]] | None


async def execute_plan(
    store: Store,
    telegram_id: int,
    client: GitHubClient,
    owner: str,
    repo: str,
    branch: str,
    plan: AgentPlan,
    progress: Progress = None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    ok = True
    last_error = ''
    for step in plan.steps:
        if progress:
            await progress(f'▶️ Step {step.order}: {step.action} — {step.description or step.args}')
        try:
            result = await execute_step(store, telegram_id, client, owner, repo, branch, step, progress)
            results.append({'step': step.order, 'action': step.action, 'ok': True, 'result': result})
            store.append_task_log(telegram_id, f'{owner}/{repo}', f'step:{step.action}', json.dumps(result, ensure_ascii=False)[:3500])
        except Exception as exc:
            ok = False
            last_error = str(exc)
            results.append({'step': step.order, 'action': step.action, 'ok': False, 'error': last_error})
            store.set_last_error(telegram_id, f'{owner}/{repo}', branch, step.action, last_error)
            if progress:
                await progress(f'❌ فشل Step {step.order}: {last_error[:700]}')
            break
    return {'ok': ok, 'results': results, 'last_error': last_error}


async def execute_step(
    store: Store,
    telegram_id: int,
    client: GitHubClient,
    owner: str,
    repo: str,
    branch: str,
    step: AgentStep,
    progress: Progress = None,
) -> dict[str, Any]:
    action = step.action.lower().strip()
    args = step.args or {}
    if action == 'analyze':
        res = await analyze_repository(client, owner, repo, branch)
        return {'message': res.message, 'details': res.details}
    if action == 'index_repo':
        res = await index_repository_memory(store, telegram_id, client, owner, repo, branch)
        return res.__dict__
    if action == 'install_workflow':
        res = await install_workflow(client, owner, repo, branch)
        return {'message': res.message, 'details': res.details}
    if action == 'run':
        command = str(args.get('command') or '').strip()
        if not command:
            raise GitHubError('خطوة run بلا command.')
        workdir = str(args.get('workdir') or '.')
        commit_changes = bool(args.get('commit_changes') or False)
        res = await run_agent_command(client, owner, repo, branch, command, workdir, commit_changes, progress=progress)
        return res.__dict__
    if action in {'read', 'write', 'append', 'prepend', 'delete', 'mkdir', 'analyze_path'}:
        if action == 'write':
            instr = f"replace {args.get('path','')}\n{args.get('content','')}"
        elif action == 'append':
            instr = f"append {args.get('path','')}\n{args.get('content','')}"
        elif action == 'prepend':
            instr = f"prepend {args.get('path','')}\n{args.get('content','')}"
        else:
            instr = f"{action} {args.get('path','')}"
        res = await apply_instruction(client, owner, repo, branch, instr)
        return {'message': res.message, 'details': res.details}
    if action == 'fix_last_error':
        err = store.get_last_error(telegram_id, f'{owner}/{repo}')
        if not err:
            return {'message': 'لا يوجد خطأ محفوظ لهذا المستودع بعد.'}
        return {'message': 'آخر خطأ محفوظ', 'error': err}
    raise GitHubError(f'خطوة غير مدعومة في الوكيل: {action}')
