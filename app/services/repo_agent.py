from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from app.services.github_client import GitHubClient, GitHubError
from app.services.agent_workflow import AGENT_WORKFLOW_CONTENT, AGENT_WORKFLOW_PATH

TEXT_EXTENSIONS = {
    '.py', '.js', '.jsx', '.ts', '.tsx', '.json', '.md', '.txt', '.yml', '.yaml', '.toml', '.ini', '.env', '.css', '.scss',
    '.html', '.xml', '.sql', '.sh', '.bash', '.zsh', '.dockerfile', '.go', '.rs', '.java', '.kt', '.php', '.rb', '.dart',
    '.c', '.cpp', '.h', '.hpp', '.cs', '.swift', '.vue', '.svelte', '.prisma', '.gitignore', '.dockerignore'
}

@dataclass
class AgentResult:
    ok: bool
    action: str
    message: str
    details: dict[str, Any]


def clean_path(path: str) -> str:
    path = path.strip().strip('`').strip().replace('\\', '/')
    path = re.sub(r'^[/]+', '', path)
    pure = PurePosixPath(path)
    if any(part in {'..', ''} for part in pure.parts):
        raise GitHubError('مسار غير آمن. لا تستخدم .. أو مسارًا فارغًا.')
    return pure.as_posix()


def detect_language(path: str, content: str = '') -> str:
    p = path.lower()
    ext = PurePosixPath(p).suffix
    mapping = {
        '.py': 'Python', '.js': 'JavaScript', '.jsx': 'React JSX', '.ts': 'TypeScript', '.tsx': 'React TSX',
        '.json': 'JSON', '.md': 'Markdown', '.yml': 'YAML', '.yaml': 'YAML', '.sql': 'SQL', '.sh': 'Shell',
        '.html': 'HTML', '.css': 'CSS', '.go': 'Go', '.rs': 'Rust', '.java': 'Java', '.php': 'PHP', '.rb': 'Ruby',
        '.dart': 'Dart', '.prisma': 'Prisma Schema', '.toml': 'TOML', '.xml': 'XML'
    }
    if PurePosixPath(p).name == 'dockerfile':
        return 'Dockerfile'
    return mapping.get(ext, 'Text/Unknown')


def split_instruction(text: str) -> tuple[str, str, str]:
    """Return action, path, body for simple natural instructions."""
    body = text.strip()
    lower = body.lower()

    patterns = [
        ('replace', r'^(?:replace|استبدل|بدل)\s+(?:file\s+|الملف\s+)?(?P<path>\S+)\s*\n(?P<body>[\s\S]*)$'),
        ('create', r'^(?:create|add|أنشئ|اضف|أضف)\s+(?:file\s+|الملف\s+)?(?P<path>\S+)\s*\n(?P<body>[\s\S]*)$'),
        ('append', r'^(?:append|ألحق|اضف_نهاية|أضف_نهاية)\s+(?P<path>\S+)\s*\n(?P<body>[\s\S]*)$'),
        ('prepend', r'^(?:prepend|اضف_بداية|أضف_بداية)\s+(?P<path>\S+)\s*\n(?P<body>[\s\S]*)$'),
        ('delete', r'^(?:delete|remove|احذف)\s+(?:file\s+|الملف\s+)?(?P<path>\S+)\s*$'),
        ('mkdir', r'^(?:mkdir|folder|أنشئ_مجلد|اضف_مجلد|أضف_مجلد)\s+(?P<path>\S+)\s*$'),
        ('read', r'^(?:read|show|اقرأ|اعرض)\s+(?P<path>\S+)\s*$'),
        ('analyze_path', r'^(?:analyze|حلل)\s+(?P<path>\S+)\s*$'),
    ]
    for action, pat in patterns:
        m = re.match(pat, body, flags=re.IGNORECASE)
        if m:
            return action, m.groupdict().get('path', ''), m.groupdict().get('body', '')

    if '```' in body:
        m = re.search(r'(?P<path>[\w./-]+)\s*\n```(?:\w+)?\n(?P<body>[\s\S]*?)```', body)
        if m:
            return 'replace', m.group('path'), m.group('body')

    if lower.startswith(('analyze', 'تحليل', 'حلل')):
        return 'analyze_repo', '', ''

    raise GitHubError('لم أفهم الأمر. استخدم صيغة مثل: replace app/main.py ثم المحتوى في السطر التالي، أو read path، أو mkdir path، أو analyze.')


async def analyze_repository(client: GitHubClient, owner: str, repo: str, branch: str) -> AgentResult:
    root = await client.list_contents(owner, repo, '', branch)
    if not isinstance(root, list):
        raise GitHubError('تعذر قراءة جذر المستودع.')
    names = {item.get('name', '').lower() for item in root}
    paths = [item.get('path', '') for item in root]
    stack: list[str] = []
    if 'package.json' in names:
        stack.append('Node.js / JavaScript / TypeScript')
    if 'next.config.js' in names or 'next.config.ts' in names:
        stack.append('Next.js')
    if 'requirements.txt' in names or 'pyproject.toml' in names:
        stack.append('Python')
    if 'dockerfile' in names:
        stack.append('Docker')
    if 'prisma' in names:
        stack.append('Prisma')
    if 'pubspec.yaml' in names:
        stack.append('Flutter/Dart')

    important: dict[str, Any] = {}
    for path in ['package.json', 'requirements.txt', 'pyproject.toml', 'Dockerfile', 'railway.json', 'vercel.json']:
        try:
            content, _ = await client.get_file(owner, repo, path, branch)
            important[path] = content[:2500]
        except Exception:
            pass

    message = '📊 تحليل المستودع\n'
    message += f'• الملفات/المجلدات في الجذر: {len(paths)}\n'
    message += f'• التقنية المتوقعة: {", ".join(stack) if stack else "غير محددة"}\n'
    message += f'• عناصر الجذر: {", ".join(paths[:30])}'
    return AgentResult(True, 'analyze_repo', message, {'stack': stack, 'root': paths, 'important': important})


async def apply_instruction(client: GitHubClient, owner: str, repo: str, branch: str, instruction: str) -> AgentResult:
    action, path, content = split_instruction(instruction)
    if action == 'analyze_repo':
        return await analyze_repository(client, owner, repo, branch)

    path = clean_path(path)
    if action == 'read':
        current, _ = await client.get_file(owner, repo, path, branch)
        lang = detect_language(path, current)
        return AgentResult(True, 'read', f'📄 {path}\nاللغة: {lang}\n\n{current[:3500]}', {'path': path, 'language': lang})

    if action == 'analyze_path':
        current, _ = await client.get_file(owner, repo, path, branch)
        lang = detect_language(path, current)
        lines = current.count('\n') + 1
        return AgentResult(True, 'analyze_path', f'🔎 تحليل الملف: {path}\n• اللغة: {lang}\n• الأسطر: {lines}\n• الحجم: {len(current.encode())} bytes', {'path': path, 'language': lang, 'lines': lines})

    if action in {'replace', 'create'}:
        await client.put_file(owner, repo, path, content, branch, f'{action.title()} {path} by Moataz Agent')
        return AgentResult(True, action, f'✅ تم حفظ الملف: {path}', {'path': path, 'bytes': len(content.encode())})

    if action == 'append':
        try:
            old, _ = await client.get_file(owner, repo, path, branch)
        except Exception:
            old = ''
        new = old.rstrip('\n') + '\n' + content.strip('\n') + '\n'
        await client.put_file(owner, repo, path, new, branch, f'Append {path} by Moataz Agent')
        return AgentResult(True, action, f'✅ تمت الإضافة في نهاية الملف: {path}', {'path': path})

    if action == 'prepend':
        try:
            old, _ = await client.get_file(owner, repo, path, branch)
        except Exception:
            old = ''
        new = content.strip('\n') + '\n' + old.lstrip('\n')
        await client.put_file(owner, repo, path, new, branch, f'Prepend {path} by Moataz Agent')
        return AgentResult(True, action, f'✅ تمت الإضافة في بداية الملف: {path}', {'path': path})

    if action == 'delete':
        await client.delete_file(owner, repo, path, branch, f'Delete {path} by Moataz Agent')
        return AgentResult(True, action, f'🗑️ تم حذف الملف: {path}', {'path': path})

    if action == 'mkdir':
        keep = path.rstrip('/') + '/.gitkeep'
        await client.put_file(owner, repo, keep, '', branch, f'Create folder {path} by Moataz Agent')
        return AgentResult(True, action, f'📁 تم إنشاء المجلد: {path}', {'path': path})

    raise GitHubError('أمر غير مدعوم.')


async def install_workflow(client: GitHubClient, owner: str, repo: str, branch: str) -> AgentResult:
    await client.put_file(owner, repo, AGENT_WORKFLOW_PATH, AGENT_WORKFLOW_CONTENT, branch, 'Install Agent Command workflow')
    return AgentResult(True, 'install_workflow', f'✅ تم تثبيت Workflow الطرفية: {AGENT_WORKFLOW_PATH}', {'path': AGENT_WORKFLOW_PATH})
