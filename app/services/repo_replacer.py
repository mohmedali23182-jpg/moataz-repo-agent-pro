from __future__ import annotations

import fnmatch
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Any

from app.services.archive import extract_archive
from app.services.project_normalizer import normalize_project, list_files_for_upload, NormalizerReport
from app.services.github_client import GitHubClient, GitHubError

ProgressCallback = Callable[[str], Awaitable[None]] | None

DEFAULT_DEPLOY_MARKERS = [
    'package.json', 'Dockerfile', 'requirements.txt', 'pyproject.toml', 'railway.json',
    'vercel.json', 'render.yaml', 'main.py', 'app.py', 'manage.py', 'go.mod', 'Cargo.toml',
    'composer.json', 'pubspec.yaml', 'next.config.js', 'next.config.ts', 'vite.config.ts',
    'vite.config.js',
]

PROTECTED_KEEP_DEFAULTS = {
    '.gitkeep',
}


@dataclass
class ReplaceOptions:
    target_dir: str = ''
    keep_patterns: list[str] = field(default_factory=list)
    no_delete: bool = False
    dry_run: bool = False
    force: bool = False
    commit_message: str = 'Replace repository content from uploaded archive'


@dataclass
class ReplacePlan:
    owner: str
    repo: str
    branch: str
    detected_root: str
    normalized_root: str
    framework: str
    deploy_files: list[str]
    new_files_count: int
    old_files_count: int
    files_to_delete: list[str]
    files_to_upload: list[str]
    target_dir: str
    keep_patterns: list[str]
    no_delete: bool
    dry_run: bool
    report: dict[str, Any]

    def telegram_text(self) -> str:
        target = self.target_dir or 'repository root'
        kept = ', '.join(self.keep_patterns) if self.keep_patterns else 'لا يوجد'
        deploy = ', '.join(self.deploy_files) if self.deploy_files else 'غير موجودة'
        return (
            '📦 <b>تقرير استبدال المستودع</b>\n'
            f'المستودع: <code>{self.owner}/{self.repo}</code>\n'
            f'الفرع: <code>{self.branch}</code>\n'
            f'جذر المشروع المكتشف: <code>{self.detected_root}</code>\n'
            f'نوع المشروع: <code>{self.framework}</code>\n'
            f'مسار الرفع: <code>{target}</code>\n'
            f'ملفات النشر: <code>{deploy}</code>\n'
            f'عدد الملفات الجديدة: <b>{self.new_files_count}</b>\n'
            f'عدد الملفات القديمة في النطاق: <b>{self.old_files_count}</b>\n'
            f'ملفات ستحذف: <b>{len(self.files_to_delete)}</b>\n'
            f'ملفات سيتم رفعها/استبدالها: <b>{len(self.files_to_upload)}</b>\n'
            f'الاحتفاظ: <code>{kept}</code>\n'
            f'حذف القديم: <b>{"لا" if self.no_delete else "نعم"}</b>\n'
            'استخدم <code>/replace --dry-run</code> للمعاينة فقط، أو <code>/replace --force</code> للتنفيذ المباشر.'
        )


@dataclass
class ReplaceResult:
    ok: bool
    dry_run: bool
    commit_sha: str | None
    commit_url: str | None
    uploaded_count: int
    deleted_count: int
    plan: ReplacePlan


def parse_replace_options(raw: str) -> ReplaceOptions:
    """Parse simple flags after /replace. Supports repo URL outside this parser in the bot."""
    tokens = raw.split()
    opts = ReplaceOptions()
    keep_mode = False
    target_next = False
    message_next = False
    message_parts: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == '--dry-run':
            opts.dry_run = True
        elif tok == '--force':
            opts.force = True
        elif tok == '--no-delete':
            opts.no_delete = True
        elif tok == '--target':
            target_next = True
        elif tok.startswith('--target='):
            opts.target_dir = tok.split('=', 1)[1].strip('/')
        elif tok == '--keep':
            keep_mode = True
        elif tok == '--message':
            message_next = True
        elif tok.startswith('--message='):
            opts.commit_message = tok.split('=', 1)[1].strip() or opts.commit_message
        elif target_next:
            opts.target_dir = tok.strip('/')
            target_next = False
        elif message_next:
            message_parts.append(tok)
        elif keep_mode:
            opts.keep_patterns.append(tok.strip('/'))
        # unknown tokens are intentionally ignored here; repo URL is handled by caller.
        i += 1
    if message_parts:
        opts.commit_message = ' '.join(message_parts).strip() or opts.commit_message
    # Always avoid weird empty patterns.
    opts.keep_patterns = [p for p in opts.keep_patterns if p]
    return opts


def _is_kept(path: str, keep_patterns: list[str]) -> bool:
    if path in PROTECTED_KEEP_DEFAULTS:
        return True
    path = path.strip('/')
    for pattern in keep_patterns:
        p = pattern.strip('/')
        if not p:
            continue
        if path == p or path.startswith(p.rstrip('/') + '/'):
            return True
        if fnmatch.fnmatch(path, p):
            return True
    return False


def _target_path(target_dir: str, rel: str) -> str:
    rel = rel.strip('/')
    target = target_dir.strip('/').strip()
    if not target or target in {'.', '/', 'root', 'ROOT'}:
        return rel
    return f'{target}/{rel}'.replace('//', '/')


def _in_target_scope(path: str, target_dir: str) -> bool:
    target = target_dir.strip('/').strip()
    if not target or target in {'.', '/', 'root', 'ROOT'}:
        return True
    return path == target or path.startswith(target + '/')


def _deploy_files(root: Path) -> list[str]:
    found: list[str] = []
    for marker in DEFAULT_DEPLOY_MARKERS:
        if (root / marker).exists():
            found.append(marker)
    return found


async def build_replace_plan(
    client: GitHubClient,
    owner: str,
    repo: str,
    branch: str,
    archive_path: Path,
    work_parent: Path,
    options: ReplaceOptions,
    progress: ProgressCallback = None,
) -> tuple[ReplacePlan, Path, dict[str, Path]]:
    if progress:
        await progress('📦 فك الضغط الآمن للملف...')
    extract_dir = work_parent / f'{archive_path.stem}_replace_extracted'
    normalized_parent = work_parent / f'{archive_path.stem}_replace_normalized'
    for d in (extract_dir, normalized_parent):
        if d.exists():
            shutil.rmtree(d)
    extract_archive(archive_path, extract_dir)

    if progress:
        await progress('🧭 اكتشاف جذر المشروع الحقيقي وترتيبه...')
    normalized_dir, normalizer_report = normalize_project(extract_dir, normalized_parent)
    local_files = list_files_for_upload(normalized_dir)
    if not local_files:
        raise GitHubError('لا توجد ملفات صالحة داخل الأرشيف بعد فك الضغط والترتيب.')

    upload_map: dict[str, Path] = {}
    for file_path in local_files:
        rel = file_path.relative_to(normalized_dir).as_posix()
        gh_path = _target_path(options.target_dir, rel)
        if not gh_path:
            continue
        upload_map[gh_path] = file_path

    if progress:
        await progress('🔎 قراءة شجرة ملفات المستودع الحالي من GitHub...')
    old_files = await client.list_repo_files(owner, repo, branch)
    scoped_old_files = [p for p in old_files if _in_target_scope(p, options.target_dir)]
    if options.no_delete:
        files_to_delete: list[str] = []
    else:
        files_to_delete = [p for p in scoped_old_files if not _is_kept(p, options.keep_patterns)]

    plan = ReplacePlan(
        owner=owner,
        repo=repo,
        branch=branch,
        detected_root=normalizer_report.original_root,
        normalized_root=str(normalized_dir),
        framework=normalizer_report.project_type,
        deploy_files=_deploy_files(normalized_dir),
        new_files_count=len(local_files),
        old_files_count=len(scoped_old_files),
        files_to_delete=files_to_delete,
        files_to_upload=sorted(upload_map.keys()),
        target_dir=options.target_dir,
        keep_patterns=options.keep_patterns,
        no_delete=options.no_delete,
        dry_run=options.dry_run,
        report=normalizer_report.to_dict(),
    )
    return plan, normalized_dir, upload_map


async def replace_repository_from_archive(
    client: GitHubClient,
    owner: str,
    repo: str,
    branch: str,
    archive_path: Path,
    work_parent: Path,
    options: ReplaceOptions,
    progress: ProgressCallback = None,
) -> ReplaceResult:
    plan, _normalized_dir, upload_map = await build_replace_plan(
        client=client,
        owner=owner,
        repo=repo,
        branch=branch,
        archive_path=archive_path,
        work_parent=work_parent,
        options=options,
        progress=progress,
    )
    if options.dry_run:
        return ReplaceResult(True, True, None, None, 0, 0, plan)

    if progress:
        await progress('🧱 إنشاء commit واحد Atomic يحذف القديم ويرفع الجديد...')
    payload = {path: file_path.read_bytes() for path, file_path in upload_map.items()}
    commit = await client.replace_files_atomic(
        owner=owner,
        repo=repo,
        branch=branch,
        upload_files=payload,
        delete_paths=plan.files_to_delete,
        message=options.commit_message,
    )
    if progress:
        await progress('✅ تم تحديث فرع المستودع بنجاح.')
    return ReplaceResult(
        ok=True,
        dry_run=False,
        commit_sha=commit.get('sha'),
        commit_url=commit.get('html_url') or commit.get('url'),
        uploaded_count=len(upload_map),
        deleted_count=len(plan.files_to_delete),
        plan=plan,
    )
