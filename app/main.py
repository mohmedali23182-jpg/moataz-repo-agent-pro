from __future__ import annotations

import hmac
import html
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from aiogram.types import Update

from app.config import get_settings
from app.bot.telegram_bot import build_bot, build_dispatcher, store
from app.services.archive import extract_archive
from app.services.github_client import GitHubAuth, GitHubClient, GitHubError, parse_repo
from app.services.supabase_client import SupabaseClient

settings = get_settings()
bot = build_bot() if settings.telegram_bot_token else None
dp = build_dispatcher()
app = FastAPI(title='Moataz Repo Agent', version='2.0.0')


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


def client_from_token(token: str | None) -> GitHubClient:
    token = token or settings.github_token
    if not token:
        raise HTTPException(400, 'GitHub token is required')
    return GitHubClient(GitHubAuth(token))


@app.on_event('startup')
async def on_startup() -> None:
    if bot and settings.public_url:
        await bot.set_webhook(settings.webhook_url, secret_token=settings.telegram_webhook_secret, drop_pending_updates=True)


@app.on_event('shutdown')
async def on_shutdown() -> None:
    if bot:
        await bot.session.close()


@app.get('/health')
async def health() -> dict[str, Any]:
    return {
        'ok': True,
        'telegram': bool(settings.telegram_bot_token),
        'webhook_path': settings.webhook_path,
        'github_token_env': bool(settings.github_token),
        'supabase': SupabaseClient().enabled(),
    }


@app.post(settings.webhook_path)
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)) -> dict[str, bool]:
    if not bot:
        raise HTTPException(500, 'TELEGRAM_BOT_TOKEN is not configured')
    if x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(401, 'Invalid Telegram secret')
    data = await request.json()
    update = Update.model_validate(data, context={'bot': bot})
    await dp.feed_update(bot, update)
    return {'ok': True}


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
    return await client.put_file(owner, repo, req.path, req.content, req.branch, req.message)


@app.post('/api/github/delete', dependencies=[Depends(require_admin)])
async def api_delete(req: DeleteRequest):
    client = client_from_token(req.token)
    owner, repo = parse_repo(req.repo)
    return await client.delete_file(owner, repo, req.path, req.branch, req.message)


@app.post('/api/github/upload-archive', dependencies=[Depends(require_admin)])
async def api_upload_archive(
    repo: str = Form(...),
    branch: str = Form('main'),
    target_dir: str = Form('uploaded_archive'),
    token: str | None = Form(None),
    file: UploadFile = File(...),
):
    client = client_from_token(token)
    owner, repo_name = parse_repo(repo)
    work = Path(settings.work_dir) / 'api'
    work.mkdir(parents=True, exist_ok=True)
    src = work / (file.filename or 'archive.bin')
    src.write_bytes(await file.read())
    extract_dir = work / (src.stem + '_extracted')
    if extract_dir.exists():
        import shutil
        shutil.rmtree(extract_dir)
    files = extract_archive(src, extract_dir)
    uploaded = []
    for p in files:
        rel = p.relative_to(extract_dir).as_posix()
        gh_path = f'{target_dir.strip("/")}/{rel}'.replace('//', '/')
        await client.put_file(owner, repo_name, gh_path, p.read_bytes(), branch, f'Upload extracted {gh_path}')
        uploaded.append(gh_path)
    return {'ok': True, 'uploaded_count': len(uploaded), 'uploaded': uploaded[:100]}


@app.get('/api/supabase/{table}', dependencies=[Depends(require_admin)])
async def api_supabase(table: str, limit: int = 20):
    return await SupabaseClient().select(table, limit=min(limit, 100))


@app.exception_handler(GitHubError)
async def github_error_handler(request: Request, exc: GitHubError):
    return JSONResponse(status_code=400, content={'ok': False, 'error': str(exc)})
