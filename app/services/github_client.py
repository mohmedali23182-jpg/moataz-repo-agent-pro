from __future__ import annotations

import base64
import time
import re
import zipfile
from io import BytesIO
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
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            r = await client.request(method, GITHUB_API + path, headers=self.headers, **kwargs)
        if r.status_code >= 400:
            detail = r.text[:1200]
            if r.status_code == 422 and 'name already exists' in detail:
                raise GitHubError('اسم المستودع موجود مسبقًا في هذا الحساب. اختر اسمًا آخر أو استخدم خيار --unique لإنشاء اسم تلقائي.')
            raise GitHubError(f'GitHub API error {r.status_code}: {detail}')
        if r.status_code == 204:
            return {}
        if not r.content:
            return {}
        try:
            return r.json()
        except Exception:
            return {'text': r.text}


    async def request_with_headers(self, method: str, path: str, **kwargs: Any) -> tuple[Any, dict[str, str]]:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            r = await client.request(method, GITHUB_API + path, headers=self.headers, **kwargs)
        headers = {k.lower(): v for k, v in r.headers.items()}
        if r.status_code >= 400:
            detail = r.text[:1200]
            raise GitHubError(f'GitHub API error {r.status_code}: {detail}')
        if r.status_code == 204 or not r.content:
            return {}, headers
        try:
            return r.json(), headers
        except Exception:
            return {'text': r.text}, headers

    async def capabilities(self) -> dict[str, Any]:
        user, headers = await self.request_with_headers('GET', '/user')
        scopes = [s.strip() for s in headers.get('x-oauth-scopes', '').split(',') if s.strip()]
        accepted = [s.strip() for s in headers.get('x-accepted-oauth-scopes', '').split(',') if s.strip()]
        repos: list[dict[str, Any]] = []
        repos_error = ''
        try:
            repos = await self.list_repos(limit=100)
        except Exception as exc:
            repos_error = str(exc)
        scope_set = set(scopes)
        can_create_repo = bool({'repo', 'public_repo'}.intersection(scope_set)) or bool(user.get('plan'))
        return {
            'viewer': user,
            'login': user.get('login'),
            'user_type': user.get('type'),
            'repos_count': len(repos),
            'repos_sample': [{'full_name': r.get('full_name'), 'private': r.get('private')} for r in repos[:20]],
            'can_create_repo': can_create_repo,
            'oauth_scopes': scopes,
            'accepted_oauth_scopes': accepted,
            'repos_error': repos_error,
        }

    async def raw_request(self, method: str, path: str, **kwargs: Any) -> bytes:
        async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
            r = await client.request(method, GITHUB_API + path, headers=self.headers, **kwargs)
        if r.status_code >= 400:
            raise GitHubError(f'GitHub API error {r.status_code}: {r.text[:1200]}')
        return r.content

    async def viewer(self) -> dict[str, Any]:
        return await self.request('GET', '/user')

    async def list_repos(self, visibility: str = 'all', limit: int = 30) -> list[dict[str, Any]]:
        return await self.request('GET', f'/user/repos?visibility={visibility}&per_page={limit}&sort=updated')

    async def create_repo(self, name: str, private: bool = True, description: str = '', auto_unique: bool = False) -> dict[str, Any]:
        safe_name = re.sub(r'[^A-Za-z0-9._-]+', '-', name.strip()).strip('-_.')
        if not safe_name:
            raise GitHubError('اسم المستودع غير صالح.')
        payload = {'name': safe_name, 'private': private, 'description': description, 'auto_init': True}
        try:
            return await self.request('POST', '/user/repos', json=payload)
        except GitHubError as e:
            if not auto_unique or 'موجود مسبقًا' not in str(e):
                raise
            viewer = await self.viewer()
            login = viewer.get('login', '')
            for i in range(2, 30):
                candidate = f'{safe_name}-{i}'
                try:
                    await self.get_repo(login, candidate)
                except Exception:
                    payload['name'] = candidate
                    return await self.request('POST', '/user/repos', json=payload)
            raise GitHubError('تعذر إيجاد اسم بديل تلقائي. اختر اسمًا مختلفًا.')

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
        query = f'?per_page={per_page}'
        if branch:
            query += f'&branch={branch}'
        return await self.request('GET', f'/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs{query}')

    async def get_workflow_run(self, owner: str, repo: str, run_id: int) -> dict[str, Any]:
        return await self.request('GET', f'/repos/{owner}/{repo}/actions/runs/{run_id}')

    async def list_workflow_jobs(self, owner: str, repo: str, run_id: int) -> dict[str, Any]:
        return await self.request('GET', f'/repos/{owner}/{repo}/actions/runs/{run_id}/jobs')

    async def get_job_logs_text(self, owner: str, repo: str, job_id: int) -> str:
        raw = await self.raw_request('GET', f'/repos/{owner}/{repo}/actions/jobs/{job_id}/logs')
        return raw.decode('utf-8', errors='replace')

    async def get_run_logs_text(self, owner: str, repo: str, run_id: int) -> str:
        try:
            jobs = await self.list_workflow_jobs(owner, repo, run_id)
            chunks = []
            for job in jobs.get('jobs', [])[:5]:
                chunks.append(await self.get_job_logs_text(owner, repo, int(job['id'])))
            return '\n\n'.join(chunks)
        except Exception:
            raw = await self.raw_request('GET', f'/repos/{owner}/{repo}/actions/runs/{run_id}/logs')
            try:
                with zipfile.ZipFile(BytesIO(raw)) as zf:
                    texts = []
                    for name in zf.namelist()[:10]:
                        texts.append(zf.read(name).decode('utf-8', errors='replace'))
                    return '\n\n'.join(texts)
            except Exception:
                return raw.decode('utf-8', errors='replace')


    async def get_branch_ref(self, owner: str, repo: str, branch: str) -> dict[str, Any]:
        return await self.request('GET', f'/repos/{owner}/{repo}/git/ref/heads/{branch}')

    async def get_commit_object(self, owner: str, repo: str, commit_sha: str) -> dict[str, Any]:
        return await self.request('GET', f'/repos/{owner}/{repo}/git/commits/{commit_sha}')

    async def get_tree_recursive(self, owner: str, repo: str, tree_sha: str) -> dict[str, Any]:
        return await self.request('GET', f'/repos/{owner}/{repo}/git/trees/{tree_sha}?recursive=1')

    async def list_repo_files(self, owner: str, repo: str, branch: str = 'main') -> list[str]:
        """Return every file path in the branch using the Git tree API.

        This is more reliable for bulk operations than walking the Contents API directory by directory.
        """
        ref = await self.get_branch_ref(owner, repo, branch)
        commit_sha = ref['object']['sha']
        commit = await self.get_commit_object(owner, repo, commit_sha)
        tree_sha = commit['tree']['sha']
        tree = await self.get_tree_recursive(owner, repo, tree_sha)
        if tree.get('truncated'):
            raise GitHubError('شجرة المستودع كبيرة جدًا وتم قطعها من GitHub API. استخدم /term أو قسّم العملية.')
        return sorted(item['path'] for item in tree.get('tree', []) if item.get('type') == 'blob')

    async def create_blob(self, owner: str, repo: str, content: bytes | str) -> dict[str, Any]:
        if isinstance(content, str):
            raw = content.encode('utf-8')
        else:
            raw = content
        return await self.request(
            'POST',
            f'/repos/{owner}/{repo}/git/blobs',
            json={'content': base64.b64encode(raw).decode(), 'encoding': 'base64'},
        )

    async def create_tree(self, owner: str, repo: str, base_tree: str, tree: list[dict[str, Any]]) -> dict[str, Any]:
        return await self.request('POST', f'/repos/{owner}/{repo}/git/trees', json={'base_tree': base_tree, 'tree': tree})

    async def create_commit_object(self, owner: str, repo: str, message: str, tree_sha: str, parents: list[str]) -> dict[str, Any]:
        return await self.request(
            'POST',
            f'/repos/{owner}/{repo}/git/commits',
            json={'message': message, 'tree': tree_sha, 'parents': parents},
        )

    async def update_branch_ref(self, owner: str, repo: str, branch: str, commit_sha: str, force: bool = False) -> dict[str, Any]:
        return await self.request(
            'PATCH',
            f'/repos/{owner}/{repo}/git/refs/heads/{branch}',
            json={'sha': commit_sha, 'force': force},
        )

    async def replace_files_atomic(
        self,
        owner: str,
        repo: str,
        branch: str,
        upload_files: dict[str, bytes | str],
        delete_paths: list[str],
        message: str,
    ) -> dict[str, Any]:
        """Replace many files in one Git commit using Git Data API.

        This avoids creating a separate commit for every file and makes /replace predictable.
        Requires Contents: Read and write on fine-grained PATs.
        """
        if not upload_files and not delete_paths:
            raise GitHubError('لا توجد تغييرات لتنفيذها.')
        ref = await self.get_branch_ref(owner, repo, branch)
        parent_sha = ref['object']['sha']
        parent_commit = await self.get_commit_object(owner, repo, parent_sha)
        base_tree = parent_commit['tree']['sha']

        tree_entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for path in sorted(set(delete_paths)):
            clean = path.strip('/')
            if clean and clean not in seen:
                tree_entries.append({'path': clean, 'mode': '100644', 'type': 'blob', 'sha': None})
                seen.add(clean)

        for path, content in sorted(upload_files.items()):
            clean = path.strip('/')
            if not clean:
                continue
            blob = await self.create_blob(owner, repo, content)
            tree_entries.append({'path': clean, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})

        new_tree = await self.create_tree(owner, repo, base_tree, tree_entries)
        commit = await self.create_commit_object(owner, repo, message, new_tree['sha'], [parent_sha])
        await self.update_branch_ref(owner, repo, branch, commit['sha'], force=False)
        return commit

    async def create_codespace(self, owner: str, repo: str, ref: str = 'main', machine: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {'ref': ref}
        if machine:
            payload['machine'] = machine
        return await self.request('POST', f'/repos/{owner}/{repo}/codespaces', json=payload)

    async def list_codespaces(self) -> dict[str, Any]:
        return await self.request('GET', '/user/codespaces?per_page=20')

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
