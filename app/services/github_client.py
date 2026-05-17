from __future__ import annotations

import base64
import time
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt

from app.config import get_settings

GITHUB_API = 'https://api.github.com'


class GitHubError(RuntimeError):
    pass


def parse_repo(value: str) -> tuple[str, str]:
    value = value.strip().replace('.git', '')
    if value.startswith('http'):
        p = urlparse(value)
        parts = [x for x in p.path.split('/') if x]
    else:
        parts = [x for x in value.split('/') if x]
    if len(parts) < 2:
        raise ValueError('صيغة المستودع غير صحيحة. استخدم owner/repo أو رابط GitHub.')
    return parts[0], parts[1]


@dataclass
class GitHubAuth:
    token: str
    mode: str = 'pat'


class GitHubClient:
    def __init__(self, auth: GitHubAuth):
        self.auth = auth
        self.headers = {
            'Authorization': f'Bearer {auth.token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'Moataz-Repo-Agent'
        }

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        timeout = kwargs.pop('timeout', 60)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.request(method, GITHUB_API + path, headers=self.headers, **kwargs)
        if r.status_code >= 400:
            detail = r.text[:1200]
            raise GitHubError(f'GitHub API error {r.status_code}: {detail}')
        if r.status_code == 204:
            return {}
        return r.json()

    async def request_bytes(self, method: str, path: str, **kwargs: Any) -> bytes:
        timeout = kwargs.pop('timeout', 120)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.request(method, GITHUB_API + path, headers=self.headers, **kwargs)
        if r.status_code >= 400:
            detail = r.text[:1200]
            raise GitHubError(f'GitHub API error {r.status_code}: {detail}')
        return r.content

    async def viewer(self) -> dict[str, Any]:
        return await self.request('GET', '/user')

    async def list_repos(self, visibility: str = 'all', limit: int = 30) -> list[dict[str, Any]]:
        return await self.request('GET', f'/user/repos?visibility={visibility}&per_page={limit}&sort=updated')

    async def create_repo(self, name: str, private: bool = True, description: str = '') -> dict[str, Any]:
        return await self.request('POST', '/user/repos', json={'name': name, 'private': private, 'description': description, 'auto_init': True})

    async def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        return await self.request('GET', f'/repos/{owner}/{repo}')

    async def list_contents(self, owner: str, repo: str, path: str = '', ref: str = 'main') -> Any:
        return await self.request('GET', f'/repos/{owner}/{repo}/contents/{path}?ref={ref}')

    async def get_file(self, owner: str, repo: str, path: str, ref: str = 'main') -> tuple[str, str]:
        data = await self.request('GET', f'/repos/{owner}/{repo}/contents/{path}?ref={ref}')
        if data.get('type') != 'file':
            raise GitHubError('المسار ليس ملفًا.')
        content = base64.b64decode(data['content']).decode('utf-8', errors='replace')
        return content, data['sha']

    async def put_file(self, owner: str, repo: str, path: str, content: bytes | str, branch: str, message: str) -> dict[str, Any]:
        if isinstance(content, str):
            raw = content.encode('utf-8')
        else:
            raw = content
        payload: dict[str, Any] = {
            'message': message,
            'content': base64.b64encode(raw).decode(),
            'branch': branch,
        }
        try:
            existing = await self.request('GET', f'/repos/{owner}/{repo}/contents/{path}?ref={branch}')
            if existing.get('sha'):
                payload['sha'] = existing['sha']
        except GitHubError:
            pass
        return await self.request('PUT', f'/repos/{owner}/{repo}/contents/{path}', json=payload)

    async def delete_file(self, owner: str, repo: str, path: str, branch: str, message: str) -> dict[str, Any]:
        existing = await self.request('GET', f'/repos/{owner}/{repo}/contents/{path}?ref={branch}')
        return await self.request('DELETE', f'/repos/{owner}/{repo}/contents/{path}', json={'message': message, 'sha': existing['sha'], 'branch': branch})

    async def create_branch(self, owner: str, repo: str, from_branch: str, new_branch: str) -> dict[str, Any]:
        ref = await self.request('GET', f'/repos/{owner}/{repo}/git/ref/heads/{from_branch}')
        sha = ref['object']['sha']
        return await self.request('POST', f'/repos/{owner}/{repo}/git/refs', json={'ref': f'refs/heads/{new_branch}', 'sha': sha})

    async def create_pull_request(self, owner: str, repo: str, head: str, base: str, title: str, body: str = '') -> dict[str, Any]:
        return await self.request('POST', f'/repos/{owner}/{repo}/pulls', json={'title': title, 'head': head, 'base': base, 'body': body})

    async def workflow_dispatch(self, owner: str, repo: str, workflow_id: str, ref: str, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.request('POST', f'/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches', json={'ref': ref, 'inputs': inputs or {}})

    async def list_workflow_runs(self, owner: str, repo: str, workflow_id: str, branch: str | None = None, per_page: int = 10) -> dict[str, Any]:
        query = f'per_page={per_page}'
        if branch:
            query += f'&branch={branch}'
        return await self.request('GET', f'/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs?{query}')

    async def get_workflow_run(self, owner: str, repo: str, run_id: int) -> dict[str, Any]:
        return await self.request('GET', f'/repos/{owner}/{repo}/actions/runs/{run_id}')

    async def get_workflow_run_logs_zip(self, owner: str, repo: str, run_id: int) -> bytes:
        return await self.request_bytes('GET', f'/repos/{owner}/{repo}/actions/runs/{run_id}/logs')

    async def add_collaborator(self, owner: str, repo: str, username: str, permission: str = 'push') -> dict[str, Any]:
        return await self.request('PUT', f'/repos/{owner}/{repo}/collaborators/{username}', json={'permission': permission})


class GitHubAppAuth:
    def __init__(self) -> None:
        self.settings = get_settings()

    def _private_key(self) -> str:
        key = self.settings.github_app_private_key.strip()
        return key.replace('\\n', '\n')

    def make_jwt(self) -> str:
        if not self.settings.github_app_id or not self._private_key():
            raise GitHubError('GITHUB_APP_ID أو GITHUB_APP_PRIVATE_KEY غير مضبوط.')
        now = int(time.time())
        payload = {'iat': now - 60, 'exp': now + 540, 'iss': self.settings.github_app_id}
        return jwt.encode(payload, self._private_key(), algorithm='RS256')

    async def installation_token(self, installation_id: str) -> str:
        token = self.make_jwt()
        headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        }
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f'{GITHUB_API}/app/installations/{installation_id}/access_tokens', headers=headers)
        if r.status_code >= 400:
            raise GitHubError(f'GitHub App token error {r.status_code}: {r.text[:1200]}')
        return r.json()['token']


def token_for_user(user: dict[str, Any]) -> str:
    settings = get_settings()
    return user.get('github_token') or settings.github_token
