from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.services.github_client import GitHubClient, GitHubError
from app.services.repo_agent import TEXT_EXTENSIONS
from app.services.store import Store

MAX_INDEX_FILES = 180
MAX_FILE_CHARS = 5000


@dataclass
class RepoIndexReport:
    owner: str
    repo: str
    branch: str
    files_indexed: int
    files_seen: int
    languages: dict[str, int]
    important_files: list[str]

    def telegram_text(self) -> str:
        langs = ', '.join(f'{k}:{v}' for k, v in sorted(self.languages.items())[:10]) or 'غير معروف'
        important = ', '.join(self.important_files[:20]) or 'لا يوجد'
        return (
            '🧠 <b>فهرسة ذاكرة المستودع</b>\n'
            f'Repo: <code>{self.owner}/{self.repo}</code>\n'
            f'Branch: <code>{self.branch}</code>\n'
            f'Files seen: <b>{self.files_seen}</b>\n'
            f'Files indexed: <b>{self.files_indexed}</b>\n'
            f'Languages: <code>{langs}</code>\n'
            f'Important: <code>{important}</code>'
        )


def _language_from_path(path: str) -> str:
    low = path.lower()
    if low.endswith(('.ts', '.tsx')):
        return 'TypeScript'
    if low.endswith(('.js', '.jsx')):
        return 'JavaScript'
    if low.endswith('.py'):
        return 'Python'
    if low.endswith(('.yml', '.yaml')):
        return 'YAML'
    if low.endswith('.json'):
        return 'JSON'
    if low.endswith(('.md', '.mdx')):
        return 'Markdown'
    if low.endswith('.sql'):
        return 'SQL'
    if low.endswith('.dart'):
        return 'Dart'
    if low.endswith('.go'):
        return 'Go'
    if low.endswith('.rs'):
        return 'Rust'
    if low.endswith('.php'):
        return 'PHP'
    if low.endswith('.java'):
        return 'Java'
    if low.endswith('.css'):
        return 'CSS'
    if low.endswith('.html'):
        return 'HTML'
    return 'Text'


def _is_indexable(path: str) -> bool:
    name = path.rsplit('/', 1)[-1].lower()
    if any(part in path for part in ['/node_modules/', '/.git/', '/dist/', '/build/', '/.next/', '/vendor/']):
        return False
    if name in {'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock'}:
        return False
    suffix = '.' + name.split('.')[-1] if '.' in name else ''
    return suffix in TEXT_EXTENSIONS or name in {'dockerfile', 'makefile', '.gitignore', '.dockerignore'}


def summarize_content(path: str, content: str) -> str:
    lines = content.splitlines()
    if len(content) <= MAX_FILE_CHARS:
        sample = content
    else:
        head = '\n'.join(lines[:120])
        tail = '\n'.join(lines[-40:])
        sample = head + '\n\n... [truncated by repo memory] ...\n\n' + tail
    digest = hashlib.sha256(content.encode('utf-8', errors='ignore')).hexdigest()[:16]
    return json.dumps({'path': path, 'sha256': digest, 'lines': len(lines), 'sample': sample[:MAX_FILE_CHARS]}, ensure_ascii=False)


async def index_repository_memory(store: Store, telegram_id: int, client: GitHubClient, owner: str, repo: str, branch: str) -> RepoIndexReport:
    files = await client.list_repo_files(owner, repo, branch)
    languages: dict[str, int] = {}
    important: list[str] = []
    indexed = 0
    for path in files:
        if path.rsplit('/', 1)[-1] in {'package.json', 'Dockerfile', 'requirements.txt', 'pyproject.toml', 'railway.json', 'vercel.json', 'render.yaml'}:
            important.append(path)
        if not _is_indexable(path):
            continue
        if indexed >= MAX_INDEX_FILES:
            break
        try:
            content, _sha = await client.get_file(owner, repo, path, branch)
        except Exception:
            continue
        lang = _language_from_path(path)
        languages[lang] = languages.get(lang, 0) + 1
        store.upsert_memory(telegram_id, f'{owner}/{repo}', branch, path, summarize_content(path, content), lang)
        indexed += 1
    store.audit(telegram_id, 'repo_index', f'{owner}/{repo}', json.dumps({'branch': branch, 'files': indexed}, ensure_ascii=False))
    return RepoIndexReport(owner, repo, branch, indexed, len(files), languages, important)


def memory_status(store: Store, telegram_id: int, repo_full: str | None = None) -> dict[str, Any]:
    return store.memory_status(telegram_id, repo_full)
