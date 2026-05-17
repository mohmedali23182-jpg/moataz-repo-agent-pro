from __future__ import annotations

import hmac
import logging
from pathlib import Path
from typing import Any

from aiogram.types import Update
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.bot.telegram_bot import build_bot, build_dispatcher
from app.config import get_settings
from app.services.archive import extract_archive
from app.services.project_normalizer import list_files_for_upload, normalize_project
from app.services.github_client import GitHubAuth, GitHubClient, GitHubError, parse_repo
from app.services.actions_runner import ActionsRunner
from app.services.repo_patch import apply_agent_instruction
from app.services.supabase_client import SupabaseClient

settings = get_settings()

logging.basicConfig(
    level=settings.log_level.upper(),
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
)

logger = logging.getLogger('moataz-repo-agent')

bot = build_bot() if settings.telegram_bot_token else None
dp = build_dispatcher()

app = FastAPI(
    title='Moataz Repo Agent',
    version='2.0.1',
)


def require_admin(authorization: str | None = Header(default=None)) -> bool:
    if not settings.admin_api_token:
        raise HTTPException(500, 'ADMIN_API_TOKEN is not configured')

    expected = f'Bearer {settings.admin_api_token}'

    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(401, 'Unauthorized')

    return True


class RepoContext(BaseModel):
    token: str | None = None
    repo: str
    branch: str = 'main'


class WriteRequest(RepoContext):
    path: str
    content: str
    message: str = 'Update by Moataz Repo Agent API'


class DeleteRequest(RepoContext):
    path: str
    message: str = 'Delete by Moataz Repo Agent API'


class CreateRepoRequest(BaseModel):
    token: str | None = None
    name: str
    private: bool = True
    description: str = 'Created by Moataz Repo Agent'


class AgentInstructionRequest(RepoContext):
    instruction: str


class TerminalRunRequest(RepoContext):
    command: str
    workdir: str = '.'
    commit_changes: bool = False
    commit_message: str = 'Apply agent terminal changes'


def client_from_token(token: str | None) -> GitHubClient:
    github_token = token or settings.github_token

    if not github_token:
        raise HTTPException(400, 'GitHub token is required')

    return GitHubClient(GitHubAuth(github_token))


@app.on_event('startup')
async def on_startup() -> None:
    logger.info('Application startup started')

    if not bot:
        logger.warning('TELEGRAM_BOT_TOKEN is empty. Telegram bot is disabled.')
        return

    if not settings.public_url:
        logger.warning('PUBLIC_URL is empty. Telegram webhook will not be registered automatically.')
        return

    logger.info('Registering Telegram webhook: %s', settings.webhook_url)

    await bot.set_webhook(
        url=settings.webhook_url,
        secret_token=settings.telegram_webhook_secret,
        drop_pending_updates=True,
    )

    logger.info('Telegram webhook registered successfully')


@app.on_event('shutdown')
async def on_shutdown() -> None:
    logger.info('Application shutdown started')

    if bot:
        await bot.session.close()


@app.get('/health')
async def health() -> dict[str, Any]:
    return {
        'ok': True,
        'telegram_enabled': bool(settings.telegram_bot_token),
        'public_url': settings.public_url,
        'webhook_path': settings.webhook_path,
        'legacy_webhook_path': settings.legacy_webhook_path,
        'webhook_url': settings.webhook_url if settings.public_url else '',
        'github_token_env': bool(settings.github_token),
        'supabase_enabled': SupabaseClient().enabled(),
        'agent_allow_terminal': settings.agent_allow_terminal,
        'agent_require_approval': settings.agent_require_approval,
        'agent_allowed_commands': settings.agent_allowed_commands,
    }


async def handle_telegram_update(
    request: Request,
    secret: str,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    logger.info('Telegram webhook POST received: path=%s', request.url.path)

    if not bot:
        logger.error('Telegram webhook called but TELEGRAM_BOT_TOKEN is not configured')
        raise HTTPException(500, 'TELEGRAM_BOT_TOKEN is not configured')

    path_secret_ok = hmac.compare_digest(
        secret,
        settings.telegram_webhook_secret,
    )

    header_secret_ok = (
        x_telegram_bot_api_secret_token is not None
        and hmac.compare_digest(
            x_telegram_bot_api_secret_token,
            settings.telegram_webhook_secret,
        )
    )

    if not path_secret_ok and not header_secret_ok:
        logger.warning(
            'Invalid Telegram webhook secret. path_secret_ok=%s header_secret_ok=%s',
            path_secret_ok,
            header_secret_ok,
        )
        raise HTTPException(401, 'Invalid Telegram secret')

    data = await request.json()

    logger.info(
        'Telegram update accepted: update_id=%s',
        data.get('update_id'),
    )

    update = Update.model_validate(
        data,
        context={'bot': bot},
    )

    await dp.feed_update(bot, update)

    logger.info(
        'Telegram update processed successfully: update_id=%s',
        data.get('update_id'),
    )

    return {'ok': True}


@app.post('/api/telegram/webhook/{secret}')
async def telegram_webhook_api(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    return await handle_telegram_update(
        request=request,
        secret=secret,
        x_telegram_bot_api_secret_token=x_telegram_bot_api_secret_token,
    )


@app.post('/telegram/webhook/{secret}')
async def telegram_webhook_legacy(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    return await handle_telegram_update(
        request=request,
        secret=secret,
        x_telegram_bot_api_secret_token=x_telegram_bot_api_secret_token,
    )


@app.get('/', response_class=HTMLResponse)
async def dashboard() -> str:
    return Path('app/web/dashboard.html').read_text(encoding='utf-8')


@app.post('/api/github/repos', dependencies=[Depends(require_admin)])
async def api_create_repo(req: CreateRepoRequest):
    client = client_from_token(req.token)
    return await client.create_repo(req.name, req.private, req.description)


@app.post('/api/github/list', dependencies=[Depends(require_admin)])
async def api_list(req: RepoContext, path: str = ''):
    client = client_from_token(req.token)
    owner, repo = parse_repo(req.repo)
    return await client.list_contents(owner, repo, path, req.branch)


@app.post('/api/github/write', dependencies=[Depends(require_admin)])
async def api_write(req: WriteRequest):
    client = client_from_token(req.token)
    owner, repo = parse_repo(req.repo)
    return await client.put_file(
        owner=owner,
        repo=repo,
        path=req.path,
        content=req.content,
        branch=req.branch,
        message=req.message,
    )


@app.post('/api/github/delete', dependencies=[Depends(require_admin)])
async def api_delete(req: DeleteRequest):
    client = client_from_token(req.token)
    owner, repo = parse_repo(req.repo)
    return await client.delete_file(
        owner=owner,
        repo=repo,
        path=req.path,
        branch=req.branch,
        message=req.message,
    )


@app.post('/api/github/upload-archive', dependencies=[Depends(require_admin)])
async def api_upload_archive(
    repo: str = Form(...),
    branch: str = Form('main'),
    target_dir: str = Form('uploaded_archive'),
    token: str | None = Form(None),
    normalize: bool = Form(True),
    file: UploadFile = File(...),
):
    client = client_from_token(token)
    owner, repo_name = parse_repo(repo)

    work = Path(settings.work_dir) / 'api'
    work.mkdir(parents=True, exist_ok=True)

    src = work / (file.filename or 'archive.bin')
    src.write_bytes(await file.read())

    extract_dir = work / f'{src.stem}_extracted'

    if extract_dir.exists():
        import shutil
        shutil.rmtree(extract_dir)

    files = extract_archive(src, extract_dir)

    upload_root = extract_dir
    report: dict[str, Any] | None = None

    if normalize:
        normalized_parent = work / f'{src.stem}_normalized_output'
        if normalized_parent.exists():
            import shutil
            shutil.rmtree(normalized_parent)
        upload_root, normalizer_report = normalize_project(extract_dir, normalized_parent)
        report = normalizer_report.to_dict()
        files = list_files_for_upload(upload_root)

    uploaded: list[str] = []
    clean_target = target_dir.strip().strip('/')

    for p in files:
        rel = p.relative_to(upload_root).as_posix()
        if clean_target in {'', '.', '/', 'root', 'ROOT'}:
            gh_path = rel.strip('/')
        else:
            gh_path = f'{clean_target}/{rel.strip("/")}'.replace('//', '/')

        await client.put_file(
            owner=owner,
            repo=repo_name,
            path=gh_path,
            content=p.read_bytes(),
            branch=branch,
            message=f'Upload normalized {gh_path}' if normalize else f'Upload extracted {gh_path}',
        )

        uploaded.append(gh_path)

    return {
        'ok': True,
        'normalized': normalize,
        'report': report,
        'uploaded_count': len(uploaded),
        'uploaded': uploaded[:100],
    }




@app.post('/api/agent/apply', dependencies=[Depends(require_admin)])
async def api_agent_apply(req: AgentInstructionRequest):
    client = client_from_token(req.token)
    result = await apply_agent_instruction(
        client=client,
        repo_value=req.repo,
        branch=req.branch,
        instruction=req.instruction,
    )
    return {
        'ok': True,
        'action': result.action,
        'path': result.path,
        'message': result.message,
    }


@app.post('/api/github/term', dependencies=[Depends(require_admin)])
async def api_github_term(req: TerminalRunRequest):
    client = client_from_token(req.token)
    owner, repo = parse_repo(req.repo)
    runner = ActionsRunner(client)
    result = await runner.dispatch_and_wait(
        owner=owner,
        repo=repo,
        branch=req.branch,
        command=req.command,
        workdir=req.workdir,
        commit_changes=req.commit_changes,
        commit_message=req.commit_message,
    )
    return {
        'ok': result.conclusion == 'success',
        'run_id': result.run_id,
        'status': result.status,
        'conclusion': result.conclusion,
        'html_url': result.html_url,
        'logs_tail': result.logs[-12000:],
    }


@app.get('/api/supabase/{table}', dependencies=[Depends(require_admin)])
async def api_supabase(table: str, limit: int = 20):
    return await SupabaseClient().select(
        table=table,
        limit=min(limit, 100),
    )


@app.exception_handler(GitHubError)
async def github_error_handler(request: Request, exc: GitHubError):
    return JSONResponse(
        status_code=400,
        content={
            'ok': False,
            'error': str(exc),
        },
    )
