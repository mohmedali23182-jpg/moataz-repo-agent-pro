from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.github_client import GitHubClient, GitHubError, parse_repo


@dataclass
class PatchResult:
    action: str
    path: str
    message: str


def _split_first_line(body: str) -> tuple[str, str]:
    lines = body.strip('\n').splitlines()
    if not lines:
        raise GitHubError('الأمر فارغ.')
    first = lines[0].strip()
    rest = '\n'.join(lines[1:])
    return first, rest


async def apply_agent_instruction(
    client: GitHubClient,
    repo_value: str,
    branch: str,
    instruction: str,
) -> PatchResult:
    owner, repo = parse_repo(repo_value)
    first, rest = _split_first_line(instruction)
    lower = first.lower()

    # Supported deterministic format examples:
    # replace app/main.py\n<full content>
    # append README.md\n<text>
    # read app/main.py
    # delete old/file.py
    # mkdir app/services/new_module
    if lower.startswith('replace ') or lower.startswith('استبدل '):
        path = first.split(maxsplit=1)[1].strip()
        if not rest.strip():
            raise GitHubError('اكتب محتوى الملف في السطور التالية بعد أمر replace.')
        await client.put_file(owner, repo, path, rest, branch, f'Agent replace {path}')
        return PatchResult('replace', path, 'تم استبدال الملف بالكامل.')

    if lower.startswith('append ') or lower.startswith('أضف إلى '):
        path = first.split(maxsplit=1)[1].strip()
        if not rest.strip():
            raise GitHubError('اكتب النص المراد إضافته في السطور التالية بعد أمر append.')
        try:
            current, _ = await client.get_file(owner, repo, path, branch)
        except Exception:
            current = ''
        new_content = current.rstrip('\n') + '\n' + rest.strip('\n') + '\n'
        await client.put_file(owner, repo, path, new_content, branch, f'Agent append {path}')
        return PatchResult('append', path, 'تمت إضافة النص إلى الملف.')

    if lower.startswith('read ') or lower.startswith('اقرأ '):
        path = first.split(maxsplit=1)[1].strip()
        content, _ = await client.get_file(owner, repo, path, branch)
        return PatchResult('read', path, content[:3500])

    if lower.startswith('delete ') or lower.startswith('احذف '):
        path = first.split(maxsplit=1)[1].strip()
        await client.delete_file(owner, repo, path, branch, f'Agent delete {path}')
        return PatchResult('delete', path, 'تم حذف الملف.')

    if lower.startswith('mkdir ') or lower.startswith('أنشئ مجلد '):
        path = first.split(maxsplit=1)[1].strip().strip('/')
        keep = f'{path}/.gitkeep'
        await client.put_file(owner, repo, keep, '', branch, f'Agent create folder {path}')
        return PatchResult('mkdir', path, 'تم إنشاء المجلد عبر ملف .gitkeep.')

    # Basic regex replace format:
    # regex path/to/file\nPATTERN\n---\nREPLACEMENT
    if lower.startswith('regex '):
        path = first.split(maxsplit=1)[1].strip()
        if '\n---\n' not in rest:
            raise GitHubError('صيغة regex: regex path\nPATTERN\n---\nREPLACEMENT')
        pattern, replacement = rest.split('\n---\n', 1)
        current, _ = await client.get_file(owner, repo, path, branch)
        new_content, count = re.subn(pattern, replacement, current, count=1, flags=re.DOTALL)
        if count == 0:
            raise GitHubError('لم يتم العثور على النمط داخل الملف.')
        await client.put_file(owner, repo, path, new_content, branch, f'Agent regex patch {path}')
        return PatchResult('regex', path, f'تم تطبيق الاستبدال. عدد العمليات: {count}')

    raise GitHubError(
        'صيغة /agent غير مفهومة. استخدم: replace path ثم المحتوى، append path ثم النص، read path، delete path، mkdir path، أو regex path.'
    )
