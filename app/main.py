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
from app.services.supabase_client import SupabaseClient, SupabaseSqlClient
from app.services.repo_agent import apply_instruction, install_workflow
from app.services.actions_runner import run_agent_command
from app.services.store import Store
from app.services.connectors.base import parse_env_text
from app.services.connectors.registry import build_connector
from app.services.ai.gateway import AIGateway
from app.services.downloads import download_direct_url, classify_url
from app.services.google_drive import GoogleDriveClient
from app.services.repo_replacer import parse_replace_options, replace_repository_from_archive, ReplaceOptions
from app.agent.planner import build_plan
from app.agent.executor import execute_plan
from app.agent.memory import index_repository_memory, memory_status
from app.services.streaming import streaming_manager

settings = get_settings()

logging.basicConfig(
    level=settings.log_level.upper(),
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
)

logger = logging.getLogger('moataz-repo-agent')
store = Store()

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


def require_agent_api(authorization: str | None = Header(default=None), x_agent_token: str | None = Header(default=None)) -> bool:
    token = settings.agent_api_token or settings.admin_api_token
    if not token:
        raise HTTPException(500, 'AGENT_API_TOKEN or ADMIN_API_TOKEN is not configured')
    bearer = f'Bearer {token}'
    ok = (authorization and hmac.compare_digest(authorization, bearer)) or (x_agent_token and hmac.compare_digest(x_agent_token, token))
    if not ok:
        raise HTTPException(401, 'Unauthorized agent token')
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
    auto_unique: bool = False


class AgentCommandRequest(RepoContext):
    instruction: str


class TerminalRequest(RepoContext):
    command: str
    workdir: str = '.'
    commit_changes: bool = False


class TaskRunRequest(RepoContext):
    telegram_id: int
    objective: str
    approve: bool = True


class MemoryIndexRequest(RepoContext):
    telegram_id: int


class SupabaseSqlRequest(BaseModel):
    sql: str


class RepoSwitchRequest(BaseModel):
    telegram_id: int
    repo: str
    branch: str | None = None


class RepoDisconnectRequest(BaseModel):
    telegram_id: int
    all: bool = False
    clear_token: bool = False


class ConnectorSaveRequest(BaseModel):
    telegram_id: int
    platform: str
    token: str
    meta: dict[str, Any] = {}


class RailwaySetVarsRequest(BaseModel):
    telegram_id: int
    project_id: str
    environment_id: str
    service_id: str | None = None
    variables: dict[str, str] = {}
    env_text: str = ''
    replace: bool = False
    skip_deploys: bool = False


class VercelSetVarsRequest(BaseModel):
    telegram_id: int
    project: str
    variables: dict[str, str] = {}
    env_text: str = ''
    target: str = 'production'


class AIConnectRequest(BaseModel):
    telegram_id: int
    provider: str
    token: str
    base_url: str = ''
    model: str = ''


class AIAskRequest(BaseModel):
    telegram_id: int
    provider: str | None = None
    prompt: str
    system: str = 'You are a senior software engineering agent.'


class DownloadUrlRequest(BaseModel):
    url: str
    filename: str = ''


class DriveUploadUrlRequest(BaseModel):
    telegram_id: int
    url: str
    folder_id: str = ''
    email: str = ''


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

    try:
        await streaming_manager.stop()
    except Exception:
        logger.exception('Failed to stop streaming manager during shutdown')

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
        'supabase_sql_enabled': SupabaseSqlClient().enabled(),
        'agent_terminal_enabled': settings.agent_allow_terminal,
        'connectors_enabled': settings.connectors_enabled,
        'ai_gateway_default_provider': settings.ai_default_provider,
        'agent_mode': settings.agent_mode,
        'memory_enabled': settings.memory_enabled,
        'sandbox_mode': settings.agent_sandbox_mode,
        'streaming_enabled': True,
        'streaming_status': streaming_manager.status(),
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




@app.get('/api/streaming/status', dependencies=[Depends(require_admin)])
async def api_streaming_status() -> dict[str, Any]:
    return {'ok': True, 'stream': streaming_manager.status()}


@app.post('/api/streaming/stop', dependencies=[Depends(require_admin)])
async def api_streaming_stop() -> dict[str, Any]:
    ok = await streaming_manager.stop()
    return {'ok': ok, 'stream': streaming_manager.status()}


@app.get('/', response_class=HTMLResponse)
async def dashboard() -> str:
    return Path('app/web/dashboard.html').read_text(encoding='utf-8')


@app.post('/api/github/repos', dependencies=[Depends(require_admin)])
async def api_create_repo(req: CreateRepoRequest):
    client = client_from_token(req.token)
    return await client.create_repo(req.name, req.private, req.description, auto_unique=req.auto_unique)


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
    target_dir: str = Form(''),
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


@app.post('/api/github/replace-archive', dependencies=[Depends(require_agent_api)])
async def api_replace_archive(
    repo: str = Form(...),
    branch: str = Form('main'),
    token: str | None = Form(None),
    flags: str = Form(''),
    dry_run: bool = Form(False),
    no_delete: bool = Form(False),
    target_dir: str = Form(''),
    keep: str = Form(''),
    file: UploadFile = File(...),
):
    """Replace repository content from an uploaded archive using one atomic Git commit."""
    client = client_from_token(token)
    owner, repo_name = parse_repo(repo)
    work = Path(settings.work_dir) / 'api-replace'
    work.mkdir(parents=True, exist_ok=True)
    src = work / (file.filename or 'archive.bin')
    src.write_bytes(await file.read())
    options = parse_replace_options(flags)
    options.dry_run = dry_run or options.dry_run
    options.no_delete = no_delete or options.no_delete
    if target_dir:
        options.target_dir = target_dir.strip('/')
    if keep:
        options.keep_patterns.extend([x.strip().strip('/') for x in keep.split(',') if x.strip()])
    result = await replace_repository_from_archive(
        client=client,
        owner=owner,
        repo=repo_name,
        branch=branch,
        archive_path=src,
        work_parent=work,
        options=options,
    )
    return {
        'ok': result.ok,
        'dry_run': result.dry_run,
        'commit_sha': result.commit_sha,
        'commit_url': result.commit_url,
        'uploaded_count': result.uploaded_count,
        'deleted_count': result.deleted_count,
        'plan': result.plan.__dict__,
    }


@app.get('/api/supabase/{table}', dependencies=[Depends(require_admin)])
async def api_supabase(table: str, limit: int = 20):
    return await SupabaseClient().select(
        table=table,
        limit=min(limit, 100),
    )


@app.post('/api/agent/apply', dependencies=[Depends(require_agent_api)])
async def api_agent_apply(req: AgentCommandRequest):
    client = client_from_token(req.token)
    owner, repo = parse_repo(req.repo)
    result = await apply_instruction(client, owner, repo, req.branch, req.instruction)
    return {'ok': result.ok, 'action': result.action, 'message': result.message, 'details': result.details}


@app.post('/api/agent/analyze', dependencies=[Depends(require_agent_api)])
async def api_agent_analyze(req: RepoContext):
    client = client_from_token(req.token)
    owner, repo = parse_repo(req.repo)
    result = await apply_instruction(client, owner, repo, req.branch, 'analyze')
    return {'ok': result.ok, 'action': result.action, 'message': result.message, 'details': result.details}


@app.post('/api/agent/install-workflow', dependencies=[Depends(require_agent_api)])
async def api_agent_install_workflow(req: RepoContext):
    client = client_from_token(req.token)
    owner, repo = parse_repo(req.repo)
    result = await install_workflow(client, owner, repo, req.branch)
    return {'ok': result.ok, 'action': result.action, 'message': result.message, 'details': result.details}


@app.post('/api/agent/term', dependencies=[Depends(require_agent_api)])
async def api_agent_terminal(req: TerminalRequest):
    client = client_from_token(req.token)
    owner, repo = parse_repo(req.repo)
    result = await run_agent_command(client, owner, repo, req.branch, req.command, req.workdir, req.commit_changes)
    return result.__dict__


@app.post('/api/supabase/sql', dependencies=[Depends(require_agent_api)])
async def api_supabase_sql(req: SupabaseSqlRequest):
    return await SupabaseSqlClient().execute(req.sql)


@app.get('/api/connections/status', dependencies=[Depends(require_agent_api)])
async def api_connections_status(telegram_id: int):
    status = store.connections_status(telegram_id)
    caps: dict[str, Any] | None = None
    user = store.get_user(telegram_id)
    token = user.get('github_token') or settings.github_token
    if token:
        try:
            caps = await GitHubClient(GitHubAuth(token)).capabilities()
        except Exception as exc:
            caps = {'error': str(exc)}
    status['github_capabilities'] = caps
    return {'ok': True, 'status': status}


@app.post('/api/repo/switch', dependencies=[Depends(require_agent_api)])
async def api_repo_switch(req: RepoSwitchRequest):
    store.set_repo(req.telegram_id, req.repo, req.branch)
    return {'ok': True, 'status': store.connections_status(req.telegram_id)}


@app.post('/api/repo/disconnect', dependencies=[Depends(require_agent_api)])
async def api_repo_disconnect(req: RepoDisconnectRequest):
    if req.all:
        store.disconnect_all(req.telegram_id, clear_token=req.clear_token)
    else:
        store.disconnect_repo(req.telegram_id)
    return {'ok': True, 'status': store.connections_status(req.telegram_id)}




@app.post('/api/connectors/save', dependencies=[Depends(require_agent_api)])
async def api_connector_save(req: ConnectorSaveRequest):
    store.set_connector_token(req.telegram_id, req.platform, req.token, req.meta)
    return {'ok': True, 'message': f'{req.platform} connector saved'}


@app.get('/api/connectors/list', dependencies=[Depends(require_agent_api)])
async def api_connector_list(telegram_id: int):
    return {'ok': True, 'connectors': store.list_connectors(telegram_id), 'ai_providers': store.list_ai_providers(telegram_id)}


@app.post('/api/connectors/railway/projects', dependencies=[Depends(require_agent_api)])
async def api_railway_projects(req: RepoDisconnectRequest):
    token, meta = store.get_connector_token(req.telegram_id, 'railway')
    connector = build_connector('railway', token, meta)
    result = await connector.projects()
    return result.__dict__


@app.post('/api/connectors/railway/set-vars', dependencies=[Depends(require_agent_api)])
async def api_railway_set_vars(req: RailwaySetVarsRequest):
    token, meta = store.get_connector_token(req.telegram_id, 'railway')
    connector = build_connector('railway', token, meta)
    variables = req.variables or parse_env_text(req.env_text)
    if not variables:
        raise HTTPException(400, 'No variables were provided')
    result = await connector.set_variables(req.project_id, req.environment_id, variables, req.service_id, req.replace, req.skip_deploys)
    return result.__dict__


@app.post('/api/connectors/vercel/projects', dependencies=[Depends(require_agent_api)])
async def api_vercel_projects(req: RepoDisconnectRequest):
    token, meta = store.get_connector_token(req.telegram_id, 'vercel')
    connector = build_connector('vercel', token, meta)
    result = await connector.projects()
    return result.__dict__


@app.post('/api/connectors/vercel/set-vars', dependencies=[Depends(require_agent_api)])
async def api_vercel_set_vars(req: VercelSetVarsRequest):
    token, meta = store.get_connector_token(req.telegram_id, 'vercel')
    connector = build_connector('vercel', token, meta)
    variables = req.variables or parse_env_text(req.env_text)
    if not variables:
        raise HTTPException(400, 'No variables were provided')
    result = await connector.set_variables(req.project, variables, req.target)
    return result.__dict__


@app.post('/api/ai/connect', dependencies=[Depends(require_agent_api)])
async def api_ai_connect(req: AIConnectRequest):
    store.set_ai_token(req.telegram_id, req.provider, req.token, req.base_url, req.model)
    return {'ok': True, 'message': f'{req.provider} AI provider saved'}


@app.post('/api/ai/ask', dependencies=[Depends(require_agent_api)])
async def api_ai_ask(req: AIAskRequest):
    provider, token, base_url, model = store.get_ai_token(req.telegram_id, req.provider)
    response = await AIGateway(provider, token, base_url, model).ask(req.prompt, req.system)
    return {'ok': True, 'provider': response.provider, 'model': response.model, 'text': response.text}



@app.post('/api/download/url', dependencies=[Depends(require_agent_api)])
async def api_download_url(req: DownloadUrlRequest):
    if classify_url(req.url) == 'google_play_listing':
        raise HTTPException(400, 'Google Play listing URLs are not direct APK files. Provide a lawful direct APK/XAPK/APKS URL or upload the file.')
    work = Path(settings.work_dir) / 'api_downloads'
    result = await download_direct_url(req.url, work, req.filename, allow_html=settings.download_allow_html)
    return {
        'ok': True,
        'filename': result.filename,
        'size_bytes': result.size_bytes,
        'content_type': result.content_type,
        'source_type': result.source_type,
        'path': str(result.path),
    }


@app.post('/api/gdrive/upload-url', dependencies=[Depends(require_agent_api)])
async def api_gdrive_upload_url(req: DriveUploadUrlRequest):
    token, meta = store.get_connector_token(req.telegram_id, 'gdrive')
    if not token:
        token, meta = store.get_connector_token(req.telegram_id, 'google_drive')
    if not token:
        raise HTTPException(400, 'Google Drive connector is not configured for this telegram_id')
    result = await download_direct_url(req.url, Path(settings.work_dir) / 'api_downloads', '', allow_html=settings.download_allow_html)
    up = await GoogleDriveClient(token, folder_id=(meta or {}).get('folder_id', '') or settings.google_drive_folder_id).upload_file(result.path, folder_id=req.folder_id, share_email=req.email)
    return {'ok': True, 'file': up.__dict__}


@app.post('/api/agent/plan', dependencies=[Depends(require_agent_api)])
async def api_agent_plan(req: TaskRunRequest):
    plan = await build_plan(store, req.telegram_id, req.objective)
    return {'ok': True, 'plan': {'objective': plan.objective, 'steps': [s.__dict__ for s in plan.steps], 'requires_approval': plan.requires_approval, 'raw': plan.raw}}


@app.post('/api/agent/run-task', dependencies=[Depends(require_agent_api)])
async def api_agent_run_task(req: TaskRunRequest):
    client = client_from_token(req.token)
    owner, repo = parse_repo(req.repo)
    plan = await build_plan(store, req.telegram_id, req.objective)
    result = await execute_plan(store, req.telegram_id, client, owner, repo, req.branch, plan)
    return {'ok': result.get('ok'), 'plan': {'objective': plan.objective, 'steps': [s.__dict__ for s in plan.steps]}, 'result': result}


@app.post('/api/agent/index-repo', dependencies=[Depends(require_agent_api)])
async def api_agent_index_repo(req: MemoryIndexRequest):
    client = client_from_token(req.token)
    owner, repo = parse_repo(req.repo)
    report = await index_repository_memory(store, req.telegram_id, client, owner, repo, req.branch)
    return {'ok': True, 'report': report.__dict__}


@app.get('/api/agent/memory/status', dependencies=[Depends(require_agent_api)])
async def api_agent_memory_status(telegram_id: int, repo_full: str = ''):
    return {'ok': True, 'status': memory_status(store, telegram_id, repo_full or None)}


@app.get('/api/agent/tasks', dependencies=[Depends(require_agent_api)])
async def api_agent_tasks(telegram_id: int):
    return {'ok': True, 'tasks': store.list_tasks(telegram_id, 20)}


@app.exception_handler(GitHubError)
async def github_error_handler(request: Request, exc: GitHubError):
    return JSONResponse(
        status_code=400,
        content={
            'ok': False,
            'error': str(exc),
        },
    )
