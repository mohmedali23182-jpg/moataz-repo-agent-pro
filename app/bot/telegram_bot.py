from __future__ import annotations

import asyncio
import html
import json
import shutil
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.client.default import DefaultBotProperties

from app.config import get_settings
from app.services.archive import extract_archive
from app.services.project_normalizer import list_files_for_upload, make_zip, normalize_project
from app.services.github_client import GitHubClient, GitHubAuth, GitHubError, parse_repo, token_for_user, GitHubAppAuth
from app.services.store import Store
from app.services.supabase_client import SupabaseClient, SupabaseSqlClient
from app.services.repo_agent import apply_instruction, install_workflow
from app.services.actions_runner import run_agent_command, validate_command
from app.services.connectors.base import parse_env_text, mask_secret
from app.services.connectors.registry import build_connector
from app.services.ai.gateway import AIGateway
from app.services.downloads import download_direct_url, android_package_report, classify_url
from app.services.google_drive import GoogleDriveClient
from app.services.repo_replacer import parse_replace_options, replace_repository_from_archive
from app.agent.planner import build_plan, AgentPlan, AgentStep
from app.agent.executor import execute_plan
from app.agent.memory import index_repository_memory, memory_status
from app.agent.sandbox import sandbox_run_github_actions
from app.services.streaming import streaming_manager
from app.bot.streaming_handlers import router as streaming_router

router = Router()
router.include_router(streaming_router)
store = Store()
settings = get_settings()
PENDING_TERMINAL: dict[int, dict] = {}
PENDING_PLANS: dict[int, dict] = {}
PENDING_STREAMS: dict[int, dict] = {}


def is_owner(user_id: int) -> bool:
    return not settings.owner_ids or user_id in settings.owner_ids


def menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text='🔗 ربط مستودع', callback_data='help_repo'), InlineKeyboardButton(text='🔑 ربط توكن', callback_data='help_token')],
        [InlineKeyboardButton(text='📂 الملفات', callback_data='cmd_ls'), InlineKeyboardButton(text='👤 الحساب', callback_data='cmd_info')],
        [InlineKeyboardButton(text='🔌 الاتصالات', callback_data='cmd_connections'), InlineKeyboardButton(text='📌 الريبو الحالي', callback_data='cmd_current_repo')],
        [InlineKeyboardButton(text='🆕 إنشاء Repo', callback_data='help_create_repo'), InlineKeyboardButton(text='🌿 إنشاء Branch', callback_data='help_branch')],
        [InlineKeyboardButton(text='📦 فك ضغط ورفع', callback_data='help_unpack'), InlineKeyboardButton(text='♻️ استبدال Repo', callback_data='help_replace')],
        [InlineKeyboardButton(text='🚀 ترتيب مشروع', callback_data='help_normalize')],
        [InlineKeyboardButton(text='⬆️ رفع ملف', callback_data='help_upload')],
        [InlineKeyboardButton(text='🧠 Supabase', callback_data='help_supabase'), InlineKeyboardButton(text='🧾 الأوامر', callback_data='help_commands')],
        [InlineKeyboardButton(text='🤖 Agent', callback_data='help_agent'), InlineKeyboardButton(text='💻 Terminal', callback_data='help_terminal')],
        [InlineKeyboardButton(text='🧠 خطة/مهام', callback_data='help_tasks'), InlineKeyboardButton(text='🧩 ذاكرة المشروع', callback_data='help_memory')],
        [InlineKeyboardButton(text='🛠️ إصلاح ذاتي', callback_data='help_autofix'), InlineKeyboardButton(text='🧪 Sandbox', callback_data='help_sandbox')],
        [InlineKeyboardButton(text='🌐 Connectors', callback_data='help_connectors'), InlineKeyboardButton(text='🧠 AI Gateway', callback_data='help_ai')],
        [InlineKeyboardButton(text='📥 Download Center', callback_data='help_downloads'), InlineKeyboardButton(text='☁️ Google Drive', callback_data='help_gdrive')],
        [InlineKeyboardButton(text='🎥 بث مباشر', callback_data='stream_menu')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def help_text() -> str:
    return '''<b>Moataz Repo Agent</b>
بوت فعلي لإدارة GitHub حسب الصلاحيات التي تعطيها له.

<b>ابدأ هكذا:</b>
1) /token github_pat_xxx
2) /switch_repo https://github.com/OWNER/REPO
3) /branch main

ثم استخدم الأوامر أو الأزرار بالأسفل.'''


def get_client_for(message_or_user_id) -> tuple[GitHubClient, dict]:
    uid = message_or_user_id if isinstance(message_or_user_id, int) else message_or_user_id.from_user.id
    user = store.get_user(uid)
    token = token_for_user(user)
    if not token:
        raise GitHubError('لا يوجد GitHub Token. استخدم /token أو ضع GITHUB_TOKEN في Railway.')
    return GitHubClient(GitHubAuth(token)), user


def repo_context(uid: int) -> tuple[GitHubClient, dict, str, str, str]:
    client, user = get_client_for(uid)
    if not user.get('repo'):
        raise GitHubError('لم تحدد المستودع. استخدم /repo owner/repo')
    owner, repo = parse_repo(user['repo'])
    branch = user.get('branch') or settings.github_default_branch
    return client, user, owner, repo, branch


async def send_error(message: Message, e: Exception) -> None:
    await message.answer('❌ <b>خطأ:</b> ' + html.escape(str(e))[:3500])


@router.message(CommandStart())
async def start(message: Message):
    if not is_owner(message.from_user.id):
        await message.answer('هذا البوت خاص. أرسل ID الخاص بك للمالك لإضافتك.')
        return
    store.upsert_user(message.from_user.id)
    await message.answer(help_text(), reply_markup=menu())


@router.message(Command('menu'))
async def show_menu(message: Message):
    await message.answer('لوحة التحكم:', reply_markup=menu())


@router.message(Command('token'))
async def set_token(message: Message):
    if not is_owner(message.from_user.id):
        return
    token = message.text.replace('/token', '', 1).strip()
    if not token:
        await message.answer('استخدم: <code>/token github_pat_xxx</code>')
        return
    store.set_token(message.from_user.id, token)
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer('✅ تم حفظ GitHub Token مشفرًا في قاعدة SQLite. لا ترسله داخل مجموعة عامة.')


@router.message(Command('clear_token'))
async def clear_token(message: Message):
    store.clear_token(message.from_user.id)
    await message.answer('✅ تم حذف التوكن الخاص بك من التخزين المحلي.')


@router.message(Command('repo'))
@router.message(Command('switch_repo'))
async def set_repo(message: Message):
    cmd = '/switch_repo' if message.text.startswith('/switch_repo') else '/repo'
    repo = message.text.replace(cmd, '', 1).strip()
    if not repo:
        await message.answer('استخدم: <code>/repo https://github.com/OWNER/REPO</code>')
        return
    parse_repo(repo)
    store.set_repo(message.from_user.id, repo)
    await message.answer(
        '✅ تم تفعيل جلسة المستودع لهذا المستخدم فقط:\n'
        f'<code>{html.escape(repo)}</code>\n'
        'لن تتداخل أوامر /agent و /term و /upload مع أي مستودع سابق.'
    )


@router.message(Command('branch'))
async def set_branch(message: Message):
    branch = message.text.replace('/branch', '', 1).strip()
    if not branch:
        await message.answer('استخدم: <code>/branch main</code>')
        return
    store.set_branch(message.from_user.id, branch)
    await message.answer(f'✅ الفرع الحالي: <code>{html.escape(branch)}</code>')


@router.message(Command('info'))
async def info(message: Message):
    try:
        client, user = get_client_for(message)
        viewer = await client.viewer()
        token_status = 'خاص بالمستخدم' if user.get('github_token') else 'من Railway GITHUB_TOKEN'
        await message.answer(
            f"👤 GitHub: <b>{html.escape(viewer.get('login',''))}</b>\n"
            f"🔐 التوكن: {token_status}\n"
            f"📦 Repo: <code>{html.escape(user.get('repo') or 'غير محدد')}</code>\n"
            f"🌿 Branch: <code>{html.escape(user.get('branch') or settings.github_default_branch)}</code>"
        )
    except Exception as e:
        await send_error(message, e)


@router.message(Command('repos'))
async def repos(message: Message):
    try:
        client, _ = get_client_for(message)
        data = await client.list_repos(limit=20)
        lines = ['<b>آخر المستودعات:</b>']
        for r in data:
            lines.append(f"• <code>{html.escape(r['full_name'])}</code> {'🔒' if r.get('private') else '🌍'}")
        await message.answer('\n'.join(lines))
    except Exception as e:
        await send_error(message, e)


async def _connections_text(uid: int, include_capabilities: bool = True) -> str:
    status = store.connections_status(uid)
    lines = ['<b>🔌 اتصالات GitHub</b>']
    lines.append(f"توكن المستخدم: {'✅' if status['has_user_token'] else '❌'}")
    lines.append(f"توكن Railway العام: {'✅' if status['has_env_token'] else '❌'}")
    session = status.get('active_session') or {}
    if session:
        lines.append('\n<b>📌 الجلسة الحالية:</b>')
        lines.append(f"Repo: <code>{html.escape(session.get('repo_url',''))}</code>")
        lines.append(f"Branch: <code>{html.escape(session.get('branch') or settings.github_default_branch)}</code>")
        lines.append(f"Token ID: <code>{html.escape(session.get('github_token_id',''))}</code>")
    else:
        lines.append('\nلا يوجد مستودع حالي. استخدم /switch_repo رابط_المستودع')
    history = status.get('known_repositories') or []
    lines.append(f"\nالمستودعات المعروفة لهذه الجلسة: <b>{len(history)}</b>")
    for item in history[:8]:
        lines.append(f"• <code>{html.escape(item.get('repo_url',''))}</code>")
    if include_capabilities:
        try:
            client, _ = get_client_for(uid)
            caps = await client.capabilities()
            scopes = ', '.join(caps.get('oauth_scopes') or []) or 'غير ظاهرة/غير متاحة'
            lines.append('\n<b>قدرات التوكن:</b>')
            lines.append(f"GitHub: <b>{html.escape(str(caps.get('login') or ''))}</b>")
            lines.append(f"Repos visible: <b>{caps.get('repos_count', 0)}</b>")
            lines.append(f"Create repo: {'✅ محتمل' if caps.get('can_create_repo') else '⚠️ غير مؤكد'}")
            lines.append(f"Scopes: <code>{html.escape(scopes)}</code>")
        except Exception as exc:
            lines.append('\nتعذر فحص صلاحيات GitHub: <code>' + html.escape(str(exc))[:500] + '</code>')
    return '\n'.join(lines)[:3900]


@router.message(Command('connections'))
async def connections(message: Message):
    if not is_owner(message.from_user.id):
        return
    await message.answer(await _connections_text(message.from_user.id))


@router.message(Command('current_repo'))
async def current_repo(message: Message):
    if not is_owner(message.from_user.id):
        return
    session = store.get_session(message.from_user.id)
    if not session:
        await message.answer('لا يوجد مستودع حالي. استخدم: <code>/switch_repo https://github.com/OWNER/REPO</code>')
        return
    await message.answer(
        '📌 <b>المستودع الحالي</b>\n'
        f"Repo: <code>{html.escape(session.get('repo_url',''))}</code>\n"
        f"Branch: <code>{html.escape(session.get('branch') or settings.github_default_branch)}</code>\n"
        f"Token: <code>{html.escape(session.get('github_token_id',''))}</code>"
    )


@router.message(Command('disconnect_repo'))
async def disconnect_repo_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    store.disconnect_repo(message.from_user.id)
    await message.answer('✅ تم فصل المستودع الحالي فقط. التوكن بقي محفوظًا، ويمكنك ربط مستودع آخر عبر /switch_repo.')


@router.message(Command('disconnect_all'))
async def disconnect_all_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    clear_token = '--clear-token' in message.text
    store.disconnect_all(message.from_user.id, clear_token=clear_token)
    await message.answer('✅ تم فصل كل جلسات المستودعات' + (' وحذف التوكن الخاص.' if clear_token else '. التوكن الخاص لم يُحذف.'))


@router.message(Command('create_repo'))
async def create_repo(message: Message):
    try:
        rest = message.text.replace('/create_repo', '', 1).strip()
        if not rest:
            await message.answer('استخدم: <code>/create_repo project-name private</code>')
            return
        parts = rest.split()
        name = parts[0]
        private = not (len(parts) > 1 and parts[1].lower() in {'public', 'عام'})
        auto_unique = '--unique' in parts or 'unique' in parts
        client, _ = get_client_for(message)
        repo = await client.create_repo(name=name, private=private, description='Created by Moataz Repo Agent', auto_unique=auto_unique)
        await message.answer(f"✅ تم إنشاء المستودع:\n{html.escape(repo.get('html_url',''))}")
    except Exception as e:
        await send_error(message, e)


@router.message(Command('ls'))
async def ls(message: Message):
    try:
        path = message.text.replace('/ls', '', 1).strip().strip('/')
        client, _, owner, repo, branch = repo_context(message.from_user.id)
        data = await client.list_contents(owner, repo, path, branch)
        if isinstance(data, dict):
            await message.answer(f"ملف: <code>{html.escape(data.get('path',''))}</code>")
            return
        lines = [f'<b>محتويات /{html.escape(path)}</b>']
        for item in data[:80]:
            icon = '📁' if item.get('type') == 'dir' else '📄'
            lines.append(f"{icon} <code>{html.escape(item.get('path',''))}</code>")
        await message.answer('\n'.join(lines)[:4000])
    except Exception as e:
        await send_error(message, e)


@router.message(Command('read'))
async def read_file(message: Message):
    try:
        path = message.text.replace('/read', '', 1).strip()
        if not path:
            await message.answer('استخدم: <code>/read path/to/file</code>')
            return
        client, _, owner, repo, branch = repo_context(message.from_user.id)
        content, _ = await client.get_file(owner, repo, path, branch)
        if len(content) > 3500:
            content = content[:3500] + '\n... مقطوع لطول الملف'
        await message.answer(f"<b>{html.escape(path)}</b>\n<pre>{html.escape(content)}</pre>")
    except Exception as e:
        await send_error(message, e)


@router.message(Command('write'))
async def write_file(message: Message):
    try:
        body = message.text.replace('/write', '', 1).strip()
        if '|' not in body:
            await message.answer('استخدم: <code>/write path/to/file | المحتوى</code>')
            return
        path, content = [x.strip() for x in body.split('|', 1)]
        client, _, owner, repo, branch = repo_context(message.from_user.id)
        await client.put_file(owner, repo, path, content, branch, f'Update {path} by Moataz Repo Agent')
        await message.answer(f'✅ تم حفظ الملف: <code>{html.escape(path)}</code>')
    except Exception as e:
        await send_error(message, e)


@router.message(Command('delete'))
async def delete_file(message: Message):
    try:
        path = message.text.replace('/delete', '', 1).strip()
        if not path:
            await message.answer('استخدم: <code>/delete path/to/file</code>')
            return
        client, _, owner, repo, branch = repo_context(message.from_user.id)
        await client.delete_file(owner, repo, path, branch, f'Delete {path} by Moataz Repo Agent')
        await message.answer(f'🗑️ تم حذف الملف: <code>{html.escape(path)}</code>')
    except Exception as e:
        await send_error(message, e)


@router.message(Command('new_branch'))
async def new_branch(message: Message):
    try:
        new = message.text.replace('/new_branch', '', 1).strip()
        if not new:
            await message.answer('استخدم: <code>/new_branch feature-name</code>')
            return
        client, _, owner, repo, branch = repo_context(message.from_user.id)
        await client.create_branch(owner, repo, branch, new)
        store.set_branch(message.from_user.id, new)
        await message.answer(f'✅ تم إنشاء الفرع وتفعيله: <code>{html.escape(new)}</code>')
    except Exception as e:
        await send_error(message, e)


@router.message(Command('pr'))
async def open_pr(message: Message):
    try:
        rest = message.text.replace('/pr', '', 1).strip()
        if '|' in rest:
            title, body = [x.strip() for x in rest.split('|', 1)]
        else:
            title, body = rest or 'Changes by Moataz Repo Agent', ''
        client, _, owner, repo, branch = repo_context(message.from_user.id)
        base = settings.github_default_branch
        pr = await client.create_pull_request(owner, repo, branch, base, title, body)
        await message.answer(f"✅ Pull Request:\n{html.escape(pr.get('html_url',''))}")
    except Exception as e:
        await send_error(message, e)


async def _download_telegram_file(bot: Bot, message: Message) -> Path:
    doc = message.reply_to_message.document if message.reply_to_message and message.reply_to_message.document else message.document
    if not doc:
        raise RuntimeError('أرسل ملفًا أو رد على ملف بالأمر.')
    if doc.file_size and doc.file_size > settings.max_upload_mb * 1024 * 1024:
        raise RuntimeError('حجم الملف أكبر من الحد المسموح.')
    tg_file = await bot.get_file(doc.file_id)
    user_dir = Path(settings.work_dir) / str(message.from_user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    dst = user_dir / doc.file_name
    await bot.download_file(tg_file.file_path, destination=dst)
    return dst


@router.message(Command('upload'))
async def upload_file(message: Message, bot: Bot):
    try:
        target = message.text.replace('/upload', '', 1).strip().strip('/')
        if not target:
            await message.answer('رد على ملف بالأمر: <code>/upload target/path.ext</code>')
            return
        src = await _download_telegram_file(bot, message)
        client, _, owner, repo, branch = repo_context(message.from_user.id)
        await client.put_file(owner, repo, target, src.read_bytes(), branch, f'Upload {target} by Moataz Repo Agent')
        await message.answer(f'✅ تم رفع الملف إلى: <code>{html.escape(target)}</code>')
    except Exception as e:
        await send_error(message, e)


def _github_target_path(target_dir: str, rel: str) -> str:
    clean_target = target_dir.strip().strip('/')
    if clean_target in {'', '.', '/', 'root', 'ROOT'}:
        return rel.strip('/')
    return f'{clean_target}/{rel.strip("/")}'.replace('//', '/')


async def _upload_directory_to_github(
    client: GitHubClient,
    owner: str,
    repo: str,
    branch: str,
    source_dir: Path,
    target_dir: str,
    message: Message,
) -> int:
    files = list_files_for_upload(source_dir)
    if not files:
        raise RuntimeError('لا توجد ملفات صالحة للرفع بعد المعالجة.')
    await message.answer(f'⬆️ جاري رفع {len(files)} ملف إلى GitHub...')
    uploaded = 0
    for p in files:
        rel = p.relative_to(source_dir).as_posix()
        gh_path = _github_target_path(target_dir, rel)
        await client.put_file(owner, repo, gh_path, p.read_bytes(), branch, f'Upload normalized {gh_path}')
        uploaded += 1
        if uploaded % 10 == 0:
            await message.answer(f'⬆️ تم رفع {uploaded}/{len(files)}')
    return uploaded


@router.message(Command('normalize'))
async def normalize_only(message: Message, bot: Bot):
    try:
        src = await _download_telegram_file(bot, message)
        extract_dir = src.parent / (src.stem + '_extracted')
        output_dir = src.parent / (src.stem + '_normalized_output')
        for d in (extract_dir, output_dir):
            if d.exists():
                shutil.rmtree(d)
        extract_archive(src, extract_dir)
        normalized_dir, report = normalize_project(extract_dir, output_dir)
        zip_path = make_zip(normalized_dir, output_dir / 'normalized-project.zip')
        await message.answer(report.telegram_text())
        await message.answer_document(document=FSInputFile(zip_path), caption='✅ ملف المشروع المرتب الجاهز للنشر')
    except Exception as e:
        await send_error(message, e)


@router.message(Command('unpack'))
async def unpack(message: Message, bot: Bot):
    try:
        raw_args = message.text.replace('/unpack', '', 1).strip()
        keep_folder = '--keep-folder' in raw_args
        raw_args = raw_args.replace('--keep-folder', '').strip()
        # Default is repository root so Railway/Vercel can detect package.json, Dockerfile, etc.
        # Use /unpack target/folder --keep-folder only when the user deliberately wants a subfolder.
        target_dir = raw_args.strip('/') if keep_folder and raw_args else ''
        if keep_folder and not target_dir:
            target_dir = 'uploaded_archive'
        src = await _download_telegram_file(bot, message)
        extract_dir = src.parent / (src.stem + '_extracted')
        output_dir = src.parent / (src.stem + '_normalized_output')
        for d in (extract_dir, output_dir):
            if d.exists():
                shutil.rmtree(d)
        extract_archive(src, extract_dir)
        normalized_dir, report = normalize_project(extract_dir, output_dir)
        client, _, owner, repo, branch = repo_context(message.from_user.id)
        files = list_files_for_upload(normalized_dir)
        deploy_files = [name for name in ['package.json','Dockerfile','requirements.txt','pyproject.toml','railway.json','vercel.json','main.py','app.py'] if (normalized_dir / name).exists()]
        await message.answer(
            report.telegram_text() +
            f"\n\n📊 <b>تقرير الرفع</b>" +
            f"\nDetected root: <code>{html.escape(report.original_root)}</code>" +
            f"\nFiles count: <b>{len(files)}</b>" +
            f"\nDeploy files: <code>{html.escape(', '.join(deploy_files) or 'غير موجودة')}</code>" +
            f"\nTarget: <code>{html.escape(target_dir or 'repository root')}</code>"
        )
        uploaded = await _upload_directory_to_github(client, owner, repo, branch, normalized_dir, target_dir, message)
        await message.answer(f'✅ اكتمل الترتيب والرفع: {uploaded} ملف إلى <code>{html.escape(target_dir or "جذر المستودع")}</code>')
    except Exception as e:
        await send_error(message, e)


async def _replace_repo_from_archive(message: Message, bot: Bot, raw_args: str) -> None:
    src = await _download_telegram_file(bot, message)
    repo_value, flags_text = parse_repo_and_body('/replace ' + raw_args, '/replace')
    # parse_repo_and_body treats any owner/repo as repo. For raw flags without repo it returns None.
    options = parse_replace_options(flags_text if repo_value else raw_args)
    client, user = get_client_for(message)
    target_repo = repo_value or user.get('repo')
    if not target_repo:
        raise GitHubError('لا يوجد مستودع حالي. استخدم /switch_repo https://github.com/OWNER/REPO أو أضف الرابط بعد /replace.')
    owner, repo = parse_repo(target_repo)
    branch = user.get('branch') or settings.github_default_branch
    progress = Progress(message, 'استبدال محتوى المستودع')
    await progress.start()
    await progress(f'📌 الهدف: {owner}/{repo}:{branch}')
    result = await replace_repository_from_archive(
        client=client,
        owner=owner,
        repo=repo,
        branch=branch,
        archive_path=src,
        work_parent=src.parent,
        options=options,
        progress=progress,
    )
    plan = result.plan
    await message.answer(plan.telegram_text()[:3900])
    if result.dry_run:
        await message.answer('🧪 Dry Run فقط. لم يتم حذف أو رفع أي ملف. للتنفيذ استخدم <code>/replace --force</code> أو بدون <code>--dry-run</code>.')
        return
    await message.answer(
        '✅ <b>تم استبدال محتوى المستودع بنجاح</b>\n'
        f'المستودع: <code>{html.escape(owner + "/" + repo)}</code>\n'
        f'الفرع: <code>{html.escape(branch)}</code>\n'
        f'تم رفع/استبدال: <b>{result.uploaded_count}</b> ملف\n'
        f'تم حذف: <b>{result.deleted_count}</b> ملف\n'
        f'Commit: <code>{html.escape(str(result.commit_sha or ""))}</code>\n'
        f'{html.escape(result.commit_url or "")}'
    )


@router.message(Command('replace'))
async def replace_repo_cmd(message: Message, bot: Bot):
    if not is_owner(message.from_user.id):
        return
    try:
        if not (message.reply_to_message and message.reply_to_message.document) and not message.document:
            await message.answer(
                'استخدم الأمر بالرد على ملف مضغوط:\n'
                '<code>/replace</code>\n'
                '<code>/replace https://github.com/OWNER/REPO --force</code>\n'
                '<code>/replace --dry-run</code>\n'
                '<code>/replace --keep README.md .env.example</code>\n'
                '<code>/replace --target apps/web</code>\n'
                '<code>/replace --no-delete</code>'
            )
            return
        raw_args = message.text.replace('/replace', '', 1).strip()
        await _replace_repo_from_archive(message, bot, raw_args)
    except Exception as e:
        await send_error(message, e)


@router.message(Command('unpack_raw'))
async def unpack_raw(message: Message, bot: Bot):
    try:
        target_dir = message.text.replace('/unpack_raw', '', 1).strip().strip('/') or 'uploaded_archive'
        src = await _download_telegram_file(bot, message)
        extract_dir = src.parent / (src.stem + '_extracted_raw')
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        files = extract_archive(src, extract_dir)
        client, _, owner, repo, branch = repo_context(message.from_user.id)
        await message.answer(f'📦 تم فك الضغط بدون ترتيب. جاري رفع {len(files)} ملف...')
        uploaded = 0
        for p in files:
            rel = p.relative_to(extract_dir).as_posix()
            gh_path = _github_target_path(target_dir, rel)
            await client.put_file(owner, repo, gh_path, p.read_bytes(), branch, f'Upload extracted {gh_path}')
            uploaded += 1
            if uploaded % 10 == 0:
                await message.answer(f'⬆️ تم رفع {uploaded}/{len(files)}')
        await message.answer(f'✅ اكتمل فك الضغط والرفع الخام: {uploaded} ملف إلى <code>{html.escape(target_dir)}</code>')
    except Exception as e:
        await send_error(message, e)


@router.message(Command('supabase'))
async def supabase_read(message: Message):
    try:
        rest = message.text.replace('/supabase', '', 1).strip()
        if not rest:
            await message.answer('استخدم: <code>/supabase table limit</code>')
            return
        parts = rest.split()
        table = parts[0]
        limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
        data = await SupabaseClient().select(table, limit=min(limit, 50))
        text = json.dumps(data, ensure_ascii=False, indent=2)[:3500]
        await message.answer(f'<pre>{html.escape(text)}</pre>')
    except Exception as e:
        await send_error(message, e)


class Progress:
    def __init__(self, message: Message, title: str = 'جاري المعالجة') -> None:
        self.message = message
        self.title = title
        self.steps: list[str] = []
        self.status_message: Message | None = None

    async def start(self) -> None:
        self.status_message = await self.message.answer(f'⌨️ <b>{html.escape(self.title)}</b>\nبدء التنفيذ...')

    async def __call__(self, text: str) -> None:
        self.steps.append(text)
        body = '\n'.join(self.steps[-8:])
        if self.status_message:
            try:
                await self.status_message.edit_text(f'⌨️ <b>{html.escape(self.title)}</b>\n{html.escape(body)}')
            except Exception:
                pass


def parse_repo_and_body(text: str, command: str) -> tuple[str | None, str]:
    body = text.replace(command, '', 1).strip()
    if not body:
        return None, ''
    parts = body.split(maxsplit=1)
    if parts and ('github.com/' in parts[0] or '/' in parts[0]):
        return parts[0], parts[1] if len(parts) > 1 else ''
    return None, body


@router.message(Command('agent'))
@router.message(Command('patch'))
async def agent_command(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        used_command = '/patch' if message.text.startswith('/patch') else '/agent'
        repo_value, instruction = parse_repo_and_body(message.text, used_command)
        if not instruction:
            await message.answer('استخدم:\n<code>/agent https://github.com/OWNER/REPO\nreplace app/file.py\nالمحتوى</code>\nأو بعد /repo استخدم الأمر بدون رابط.')
            return
        progress = Progress(message, 'Agent يعدل المستودع')
        await progress.start()
        await progress('🔐 قراءة التوكن وسياق المستودع...')
        client, user = get_client_for(message)
        if repo_value:
            owner, repo = parse_repo(repo_value)
        else:
            if not user.get('repo'):
                raise GitHubError('حدد مستودعًا في الأمر أو استخدم /repo أولًا.')
            owner, repo = parse_repo(user['repo'])
        branch = user.get('branch') or settings.github_default_branch
        await progress(f'📦 المستودع: {owner}/{repo} على الفرع {branch}')
        result = await apply_instruction(client, owner, repo, branch, instruction)
        await progress('✅ انتهى التنفيذ')
        await message.answer(html.escape(result.message)[:3900])
    except Exception as e:
        await send_error(message, e)


@router.message(Command('analyze_repo'))
async def analyze_repo_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        repo_value = message.text.replace('/analyze_repo', '', 1).strip()
        progress = Progress(message, 'تحليل المستودع')
        await progress.start()
        await progress('🔎 قراءة بنية المشروع...')
        client, user = get_client_for(message)
        if repo_value:
            owner, repo = parse_repo(repo_value)
        else:
            if not user.get('repo'):
                raise GitHubError('استخدم /analyze_repo https://github.com/OWNER/REPO أو اربط مستودعًا بـ /repo.')
            owner, repo = parse_repo(user['repo'])
        branch = user.get('branch') or settings.github_default_branch
        result = await apply_instruction(client, owner, repo, branch, 'analyze')
        await progress('✅ اكتمل التحليل')
        await message.answer(html.escape(result.message)[:3900])
    except Exception as e:
        await send_error(message, e)


@router.message(Command('install_workflow'))
async def install_workflow_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        repo_value = message.text.replace('/install_workflow', '', 1).strip()
        progress = Progress(message, 'تثبيت Workflow الطرفية')
        await progress.start()
        client, user = get_client_for(message)
        if repo_value:
            owner, repo = parse_repo(repo_value)
        else:
            if not user.get('repo'):
                raise GitHubError('حدد مستودعًا أو استخدم /repo أولًا.')
            owner, repo = parse_repo(user['repo'])
        branch = user.get('branch') or settings.github_default_branch
        await progress('🧩 رفع .github/workflows/agent-command.yml...')
        result = await install_workflow(client, owner, repo, branch)
        await progress('✅ تم التثبيت')
        await message.answer(html.escape(result.message))
    except Exception as e:
        await send_error(message, e)


@router.message(Command('term'))
async def terminal_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        repo_value, command = parse_repo_and_body(message.text, '/term')
        if not command:
            await message.answer('استخدم:\n<code>/term https://github.com/OWNER/REPO\nnpm run build</code>')
            return
        validate_command(command)
        client, user = get_client_for(message)
        target_repo = repo_value or user.get('repo')
        if not target_repo:
            raise GitHubError('حدد مستودعًا في الأمر أو استخدم /repo أولًا.')
        owner, repo = parse_repo(target_repo)
        branch = user.get('branch') or settings.github_default_branch
        payload = {'repo': target_repo, 'owner': owner, 'repo_name': repo, 'branch': branch, 'command': command, 'workdir': settings.agent_default_workdir}
        if settings.agent_require_approval:
            PENDING_TERMINAL[message.from_user.id] = payload
            await message.answer(
                '⚠️ الأمر جاهز وينتظر الموافقة:\n'
                f'📦 <code>{html.escape(owner + "/" + repo)}</code>\n'
                f'🌿 <code>{html.escape(branch)}</code>\n'
                f'💻 <code>{html.escape(command)}</code>\n\n'
                'أرسل <code>/approve</code> للتنفيذ أو <code>/cancel_term</code> للإلغاء.'
            )
            return
        await _execute_terminal_payload(message, payload)
    except Exception as e:
        await send_error(message, e)


async def _execute_terminal_payload(message: Message, payload: dict) -> None:
    client, _ = get_client_for(message)
    progress = Progress(message, 'Terminal عبر GitHub Actions')
    await progress.start()
    await progress('🔐 التحقق من الصلاحيات والأمر...')
    result = await run_agent_command(
        client=client,
        owner=payload['owner'],
        repo=payload['repo_name'],
        branch=payload['branch'],
        command=payload['command'],
        workdir=payload.get('workdir') or '.',
        commit_changes=False,
        progress=progress,
    )
    if not result.ok:
        store.set_last_error(message.from_user.id, f"{payload['owner']}/{payload['repo_name']}", payload['branch'], 'terminal', (result.logs or str(result.conclusion))[-5000:])
    icon = '✅' if result.ok else '❌'
    await message.answer(
        f'{icon} <b>نتيجة الطرفية</b>\n'
        f'Run ID: <code>{result.run_id}</code>\n'
        f'الحالة: <code>{html.escape(str(result.status))}</code>\n'
        f'النتيجة: <code>{html.escape(str(result.conclusion))}</code>\n'
        f'الرابط: {html.escape(result.html_url or "")}\n\n'
        f'<pre>{html.escape(result.logs[-3500:])}</pre>'
    )


@router.message(Command('approve'))
async def approve_terminal(message: Message):
    if not is_owner(message.from_user.id):
        return
    payload = PENDING_TERMINAL.pop(message.from_user.id, None)
    if not payload:
        await message.answer('لا يوجد أمر طرفية ينتظر الموافقة.')
        return
    try:
        await _execute_terminal_payload(message, payload)
    except Exception as e:
        await send_error(message, e)


@router.message(Command('cancel_term'))
async def cancel_terminal(message: Message):
    PENDING_TERMINAL.pop(message.from_user.id, None)
    await message.answer('تم إلغاء أمر الطرفية المعلّق.')


# -------------------------
# Manus-like Planner / Memory / Self-healing commands
# -------------------------

def _plan_to_dict(plan: AgentPlan) -> dict:
    return {
        'objective': plan.objective,
        'requires_approval': plan.requires_approval,
        'raw': plan.raw,
        'steps': [{'order': s.order, 'action': s.action, 'args': s.args, 'description': s.description} for s in plan.steps],
    }


def _plan_from_dict(data: dict) -> AgentPlan:
    steps = [AgentStep(int(x.get('order') or i + 1), str(x.get('action') or ''), x.get('args') or {}, x.get('description') or '') for i, x in enumerate(data.get('steps') or [])]
    return AgentPlan(str(data.get('objective') or ''), steps, bool(data.get('requires_approval', True)), str(data.get('raw') or ''))


def _new_task_id(uid: int) -> str:
    import secrets
    return f'task_{uid}_{secrets.token_hex(4)}'


@router.message(Command('plan'))
async def plan_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        repo_value, objective = parse_repo_and_body(message.text, '/plan')
        if not objective:
            await message.answer('استخدم:\n<code>/plan https://github.com/OWNER/REPO\nافحص المشروع وشغل build واقترح الإصلاح</code>')
            return
        client, user = get_client_for(message)
        target_repo = repo_value or user.get('repo')
        if not target_repo:
            raise GitHubError('لا يوجد مستودع. استخدم /switch_repo أو ضع الرابط بعد /plan.')
        owner, repo = parse_repo(target_repo)
        branch = user.get('branch') or settings.github_default_branch
        progress = Progress(message, 'تخطيط مهمة Agent')
        await progress.start()
        await progress('🧠 بناء خطة تنفيذ آمنة...')
        plan = await build_plan(store, message.from_user.id, objective)
        task_id = _new_task_id(message.from_user.id)
        repo_full = f'{owner}/{repo}'
        store.create_task(task_id, message.from_user.id, repo_full, branch, 'planned', objective, _plan_to_dict(plan))
        PENDING_PLANS[message.from_user.id] = {'task_id': task_id, 'repo': target_repo, 'owner': owner, 'repo_name': repo, 'branch': branch, 'plan': _plan_to_dict(plan)}
        await progress('✅ الخطة جاهزة وتنتظر موافقتك')
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='✅ تنفيذ الخطة', callback_data=f'approve_plan:{task_id}'), InlineKeyboardButton(text='❌ إلغاء', callback_data=f'cancel_task:{task_id}')],
            [InlineKeyboardButton(text='📜 سجلات المهام', callback_data='cmd_task_logs')],
        ])
        await message.answer(f'<b>Task ID:</b> <code>{task_id}</code>\n' + plan.telegram_text(), reply_markup=keyboard)
    except Exception as e:
        await send_error(message, e)


@router.message(Command('run_task'))
@router.message(Command('task'))
async def run_task_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        cmd = '/run_task' if message.text.startswith('/run_task') else '/task'
        repo_value, objective = parse_repo_and_body(message.text, cmd)
        if not objective:
            await message.answer('استخدم:\n<code>/task https://github.com/OWNER/REPO\n1. حلل المشروع\n2. شغل build\n3. أصلح الخطأ</code>')
            return
        client, user = get_client_for(message)
        target_repo = repo_value or user.get('repo')
        if not target_repo:
            raise GitHubError('لا يوجد مستودع. استخدم /switch_repo أو ضع الرابط بعد /task.')
        owner, repo = parse_repo(target_repo)
        branch = user.get('branch') or settings.github_default_branch
        progress = Progress(message, 'Agent Task Runner')
        await progress.start()
        await progress('🧠 بناء الخطة...')
        plan = await build_plan(store, message.from_user.id, objective)
        task_id = _new_task_id(message.from_user.id)
        repo_full = f'{owner}/{repo}'
        store.create_task(task_id, message.from_user.id, repo_full, branch, 'planned', objective, _plan_to_dict(plan))
        if settings.agent_require_plan_approval:
            PENDING_PLANS[message.from_user.id] = {'task_id': task_id, 'repo': target_repo, 'owner': owner, 'repo_name': repo, 'branch': branch, 'plan': _plan_to_dict(plan)}
            await progress('⏸️ الخطة تنتظر الموافقة')
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✅ تنفيذ الآن', callback_data=f'approve_plan:{task_id}'), InlineKeyboardButton(text='❌ إلغاء', callback_data=f'cancel_task:{task_id}')]])
            await message.answer(f'<b>Task ID:</b> <code>{task_id}</code>\n' + plan.telegram_text(), reply_markup=keyboard)
            return
        store.update_task(task_id, 'running')
        result = await execute_plan(store, message.from_user.id, client, owner, repo, branch, plan, progress)
        store.update_task(task_id, 'done' if result.get('ok') else 'failed', result)
        await progress('✅ انتهت المهمة' if result.get('ok') else '❌ فشلت المهمة')
        await message.answer('<pre>' + html.escape(json.dumps(result, ensure_ascii=False, indent=2)[-3500:]) + '</pre>')
    except Exception as e:
        await send_error(message, e)


async def _execute_pending_plan(message: Message, task_id: str) -> None:
    payload = PENDING_PLANS.get(message.from_user.id)
    if not payload or payload.get('task_id') != task_id:
        task = store.get_task(task_id)
        if not task:
            raise GitHubError('لا توجد خطة معلقة بهذا Task ID.')
        owner, repo = parse_repo(task['repo_full'])
        payload = {'task_id': task_id, 'owner': owner, 'repo_name': repo, 'branch': task.get('branch') or settings.github_default_branch, 'plan': task['plan_json']}
    client, _user = get_client_for(message)
    plan = _plan_from_dict(payload['plan'])
    progress = Progress(message, f'تنفيذ {task_id}')
    await progress.start()
    store.update_task(task_id, 'running')
    result = await execute_plan(store, message.from_user.id, client, payload['owner'], payload['repo_name'], payload['branch'], plan, progress)
    store.update_task(task_id, 'done' if result.get('ok') else 'failed', result)
    PENDING_PLANS.pop(message.from_user.id, None)
    await progress('✅ انتهت المهمة' if result.get('ok') else '❌ فشلت المهمة')
    await message.answer('<b>Task result:</b> <code>' + html.escape(task_id) + '</code>\n<pre>' + html.escape(json.dumps(result, ensure_ascii=False, indent=2)[-3500:]) + '</pre>')


@router.message(Command('approve_plan'))
async def approve_plan_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        task_id = message.text.replace('/approve_plan', '', 1).strip()
        if not task_id:
            pending = PENDING_PLANS.get(message.from_user.id) or {}
            task_id = pending.get('task_id', '')
        if not task_id:
            await message.answer('لا توجد خطة معلّقة. استخدم /plan أو /task أولًا.')
            return
        await _execute_pending_plan(message, task_id)
    except Exception as e:
        await send_error(message, e)


@router.message(Command('task_status'))
async def task_status_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    task_id = message.text.replace('/task_status', '', 1).strip()
    if task_id:
        task = store.get_task(task_id)
        await message.answer('<pre>' + html.escape(json.dumps(task or {'error': 'not found'}, ensure_ascii=False, indent=2)[:3500]) + '</pre>')
        return
    tasks = store.list_tasks(message.from_user.id, 10)
    lines = ['<b>آخر المهام:</b>']
    for t in tasks:
        lines.append(f"• <code>{html.escape(t['task_id'])}</code> {html.escape(t['status'])} — {html.escape(t.get('repo_full') or '')}")
    await message.answer('\n'.join(lines) if len(lines) > 1 else 'لا توجد مهام بعد.')


@router.message(Command('task_logs'))
async def task_logs_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    session = store.get_session(message.from_user.id)
    repo_full = f"{session.get('owner')}/{session.get('repo')}" if session else None
    rows = store.task_logs(message.from_user.id, repo_full, 30)
    if not rows:
        await message.answer('لا توجد سجلات بعد.')
        return
    lines = ['<b>سجلات Agent:</b>']
    for r in rows[:20]:
        lines.append(f"• <code>{html.escape(r.get('step') or '')}</code> {html.escape((r.get('message') or '')[:250])}")
    await message.answer('\n'.join(lines)[:3900])


@router.message(Command('task_cancel'))
async def task_cancel_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    task_id = message.text.replace('/task_cancel', '', 1).strip()
    if task_id:
        store.update_task(task_id, 'cancelled')
    PENDING_PLANS.pop(message.from_user.id, None)
    await message.answer('✅ تم إلغاء المهمة أو الخطة المعلقة.')


@router.message(Command('index_repo'))
async def index_repo_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        repo_value = message.text.replace('/index_repo', '', 1).strip()
        client, user = get_client_for(message)
        target = repo_value or user.get('repo')
        if not target:
            raise GitHubError('حدد مستودعًا أو استخدم /switch_repo أولًا.')
        owner, repo = parse_repo(target)
        branch = user.get('branch') or settings.github_default_branch
        progress = Progress(message, 'فهرسة ذاكرة المستودع')
        await progress.start()
        await progress('🔎 قراءة ملفات المستودع وبناء ذاكرة SQLite...')
        report = await index_repository_memory(store, message.from_user.id, client, owner, repo, branch)
        await progress('✅ اكتملت الفهرسة')
        await message.answer(report.telegram_text())
    except Exception as e:
        await send_error(message, e)


@router.message(Command('memory_status'))
async def memory_status_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    session = store.get_session(message.from_user.id)
    repo_full = f"{session.get('owner')}/{session.get('repo')}" if session else None
    status = memory_status(store, message.from_user.id, repo_full)
    await message.answer('<pre>' + html.escape(json.dumps(status, ensure_ascii=False, indent=2)) + '</pre>')


@router.message(Command('forget_repo_memory'))
async def forget_repo_memory_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    session = store.get_session(message.from_user.id)
    repo_full = f"{session.get('owner')}/{session.get('repo')}" if session else None
    n = store.forget_memory(message.from_user.id, repo_full)
    await message.answer(f'✅ تم حذف {n} عنصرًا من ذاكرة هذا المستودع.')


@router.message(Command('fix_last_error'))
async def fix_last_error_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        client, user = get_client_for(message)
        if not user.get('repo'):
            raise GitHubError('حدد مستودعًا أولًا.')
        owner, repo = parse_repo(user['repo'])
        repo_full = f'{owner}/{repo}'
        err = store.get_last_error(message.from_user.id, repo_full)
        if not err:
            await message.answer('لا يوجد خطأ محفوظ. شغل /term أو /task أولًا ثم استخدم هذا الأمر.')
            return
        provider, token, base_url, model = store.get_ai_token(message.from_user.id)
        prompt = 'حلل هذا الخطأ في مشروع GitHub واقترح Patch عملي مختصر، ولا تخترع ملفات غير مذكورة:\n' + json.dumps(err, ensure_ascii=False, indent=2)
        if token:
            response = await AIGateway(provider, token, base_url, model).ask(prompt, 'You are a senior debugging agent. Return concrete steps and code-level hints.', 0.1)
            await message.answer('🛠️ <b>تحليل الخطأ الأخير</b>\n' + html.escape(response.text[:3500]))
        else:
            await message.answer('🛠️ <b>آخر خطأ محفوظ</b>\n<pre>' + html.escape(json.dumps(err, ensure_ascii=False, indent=2)[:3500]) + '</pre>\nاربط AI عبر /ai_connect ليقترح الإصلاح تلقائيًا.')
    except Exception as e:
        await send_error(message, e)


@router.message(Command('autofix'))
async def autofix_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        client, user = get_client_for(message)
        if not user.get('repo'):
            raise GitHubError('حدد مستودعًا أولًا.')
        owner, repo = parse_repo(user['repo'])
        branch = user.get('branch') or settings.github_default_branch
        repo_full = f'{owner}/{repo}'
        err = store.get_last_error(message.from_user.id, repo_full)
        if not err:
            await message.answer('لا يوجد خطأ محفوظ لهذا المستودع.')
            return
        objective = 'اقرأ آخر خطأ، افحص المشروع، اقترح إصلاحًا آمنًا، ثم شغل اختبار build. الخطأ: ' + str(err.get('error',''))[:1200]
        progress = Progress(message, 'Autofix Planner')
        await progress.start()
        await progress('🧠 بناء خطة إصلاح ذاتي آمنة...')
        plan = await build_plan(store, message.from_user.id, objective)
        task_id = _new_task_id(message.from_user.id)
        store.create_task(task_id, message.from_user.id, repo_full, branch, 'planned', objective, _plan_to_dict(plan))
        PENDING_PLANS[message.from_user.id] = {'task_id': task_id, 'repo': user['repo'], 'owner': owner, 'repo_name': repo, 'branch': branch, 'plan': _plan_to_dict(plan)}
        await progress('⏸️ الخطة تنتظر موافقتك')
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✅ تنفيذ خطة الإصلاح', callback_data=f'approve_plan:{task_id}'), InlineKeyboardButton(text='❌ إلغاء', callback_data=f'cancel_task:{task_id}')]])
        await message.answer(f'<b>Task ID:</b> <code>{task_id}</code>\n' + plan.telegram_text(), reply_markup=keyboard)
    except Exception as e:
        await send_error(message, e)


@router.message(Command('sandbox_run'))
@router.message(Command('codeact'))
async def sandbox_run_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        cmd = '/codeact' if message.text.startswith('/codeact') else '/sandbox_run'
        repo_value, command = parse_repo_and_body(message.text, cmd)
        if not command:
            await message.answer('استخدم:\n<code>/sandbox_run https://github.com/OWNER/REPO\npython -m compileall app</code>')
            return
        validate_command(command)
        client, user = get_client_for(message)
        target = repo_value or user.get('repo')
        if not target:
            raise GitHubError('حدد مستودعًا أو استخدم /switch_repo أولًا.')
        owner, repo = parse_repo(target)
        branch = user.get('branch') or settings.github_default_branch
        progress = Progress(message, 'Sandbox عبر GitHub Actions')
        await progress.start()
        result = await sandbox_run_github_actions(client, owner, repo, branch, command, settings.agent_default_workdir, progress)
        if not result.ok:
            store.set_last_error(message.from_user.id, f'{owner}/{repo}', branch, 'sandbox_run', result.logs[-5000:])
        await message.answer(('✅' if result.ok else '❌') + ' <b>Sandbox result</b>\n<pre>' + html.escape(result.logs[-3500:]) + '</pre>')
    except Exception as e:
        await send_error(message, e)


@router.message(Command('codespace'))
async def codespace_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        rest = message.text.replace('/codespace', '', 1).strip()
        client, user = get_client_for(message)
        if rest.startswith('list') or not rest:
            data = await client.list_codespaces()
            lines = ['<b>Codespaces:</b>']
            for c in data.get('codespaces', [])[:10]:
                lines.append(f"• <code>{html.escape(c.get('display_name') or c.get('name',''))}</code> {html.escape(c.get('state',''))} {html.escape(c.get('web_url',''))}")
            await message.answer('\n'.join(lines)[:3900])
            return
        owner, repo = parse_repo(rest or user.get('repo', ''))
        branch = user.get('branch') or settings.github_default_branch
        data = await client.create_codespace(owner, repo, ref=branch)
        await message.answer(f"✅ تم طلب إنشاء Codespace:\n{html.escape(data.get('web_url','افتحه من GitHub Codespaces'))}")
    except Exception as e:
        await send_error(message, e)


@router.message(Command('supabase_sql'))
async def supabase_sql_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        sql = message.text.replace('/supabase_sql', '', 1).strip()
        if not sql:
            await message.answer('استخدم: <code>/supabase_sql select now();</code>\nيحتاج SUPABASE_ALLOW_SQL=true و DATABASE_URL/DIRECT_URL.')
            return
        progress = Progress(message, 'Supabase SQL')
        await progress.start()
        await progress('🧠 تنفيذ SQL على قاعدة تملك صلاحيتها...')
        data = await SupabaseSqlClient().execute(sql)
        await progress('✅ اكتمل التنفيذ')
        await message.answer('<pre>' + html.escape(json.dumps(data, ensure_ascii=False, indent=2)[:3500]) + '</pre>')
    except Exception as e:
        await send_error(message, e)





# -------------------------
# Download Center + Google Drive
# -------------------------

def _tmp_user_dir(uid: int) -> Path:
    d = Path(settings.work_dir) / str(uid) / 'downloads'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _drive_client_for(uid: int) -> GoogleDriveClient:
    token, meta = store.get_connector_token(uid, 'gdrive')
    if not token:
        token, meta = store.get_connector_token(uid, 'google_drive')
    if not token:
        raise RuntimeError('لا يوجد اتصال Google Drive. استخدم /gdrive_connect ACCESS_TOKEN أو رد على service-account-json بالأمر /gdrive_connect folder_id=...')
    return GoogleDriveClient(token, folder_id=(meta or {}).get('folder_id', '') or settings.google_drive_folder_id)


@router.message(Command('download_file'))
@router.message(Command('apk'))
async def download_file_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        cmd = '/apk' if message.text.startswith('/apk') else '/download_file'
        body = message.text.replace(cmd, '', 1).strip()
        if not body:
            await message.answer('استخدم: <code>/download_file DIRECT_URL [filename]</code> أو <code>/apk DIRECT_APK_URL</code>')
            return
        parts = body.split(maxsplit=1)
        url = parts[0]
        preferred_name = parts[1].strip() if len(parts) > 1 else ''
        progress = Progress(message, 'Download Center')
        await progress.start()
        await progress('🔎 فحص الرابط والمصدر...')
        if classify_url(url) == 'google_play_listing':
            await message.answer(
                '⚠️ هذا رابط صفحة Google Play. لا يوجد تنزيل APK مباشر رسمي من رابط المتجر. '
                'أرسل رابط ملف مباشر تملك حق تحميله مثل .apk/.xapk/.apks أو GitHub Release asset. '
                'يمكن للبوت تحميل الروابط المباشرة ورفعها إلى GitHub أو Google Drive.'
            )
            return
        await progress('⬇️ تحميل الملف المباشر...')
        result = await download_direct_url(url, _tmp_user_dir(message.from_user.id), preferred_name, allow_html=settings.download_allow_html)
        await progress('✅ اكتمل التحميل')
        caption = f'✅ تم التحميل: <code>{html.escape(result.filename)}</code>\nالحجم: <b>{result.size_bytes/1024/1024:.2f} MB</b>'
        if cmd == '/apk':
            caption += '\n\n' + android_package_report(result.path)
        await message.answer_document(FSInputFile(result.path), caption=caption[:1000])
    except Exception as e:
        await send_error(message, e)


@router.message(Command('download_to_repo'))
async def download_to_repo_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        body = message.text.replace('/download_to_repo', '', 1).strip()
        parts = body.split(maxsplit=1)
        if not parts:
            await message.answer('استخدم: <code>/download_to_repo DIRECT_URL path/in/repo.apk</code>')
            return
        url = parts[0]
        target = parts[1].strip().strip('/') if len(parts) > 1 else ''
        progress = Progress(message, 'تحميل ورفع إلى GitHub')
        await progress.start()
        await progress('⬇️ تحميل الملف...')
        result = await download_direct_url(url, _tmp_user_dir(message.from_user.id), '', allow_html=settings.download_allow_html)
        target = target or result.filename
        await progress('🔐 قراءة سياق GitHub...')
        client, _, owner, repo, branch = repo_context(message.from_user.id)
        await progress(f'⬆️ رفع إلى {owner}/{repo}:{branch} => {target}')
        await client.put_file(owner, repo, target, result.path.read_bytes(), branch, f'Upload downloaded file {target}')
        await progress('✅ اكتمل')
        await message.answer(f'✅ تم تحميل الملف ورفعه إلى GitHub: <code>{html.escape(target)}</code>')
    except Exception as e:
        await send_error(message, e)


@router.message(Command('gdrive_connect'))
async def gdrive_connect_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        body = message.text.replace('/gdrive_connect', '', 1).strip()
        token = ''
        meta: dict = {}
        if message.reply_to_message and message.reply_to_message.text:
            token = message.reply_to_message.text.strip()
            for part in body.split():
                if part.startswith('folder_id='):
                    meta['folder_id'] = part.split('=', 1)[1]
        else:
            parts = body.split()
            if not parts:
                await message.answer('استخدم: <code>/gdrive_connect ACCESS_TOKEN [folder_id=FOLDER_ID]</code> أو رد على JSON حساب خدمة بالأمر <code>/gdrive_connect folder_id=...</code>')
                return
            token = parts[0]
            for part in parts[1:]:
                if part.startswith('folder_id='):
                    meta['folder_id'] = part.split('=', 1)[1]
        store.set_connector_token(message.from_user.id, 'gdrive', token, meta)
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer('✅ تم حفظ اتصال Google Drive مشفرًا. اختبره عبر /gdrive_status')
    except Exception as e:
        await send_error(message, e)


@router.message(Command('gdrive_status'))
async def gdrive_status_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        progress = Progress(message, 'Google Drive')
        await progress.start()
        await progress('🔐 فحص الاتصال...')
        data = await _drive_client_for(message.from_user.id).about()
        user = data.get('user') or {}
        quota = data.get('storageQuota') or {}
        await progress('✅ الاتصال يعمل')
        await message.answer(
            '☁️ <b>Google Drive متصل</b>\n'
            f"User: <code>{html.escape(user.get('emailAddress',''))}</code>\n"
            f"Used: <code>{html.escape(str(quota.get('usage','')))}</code>"
        )
    except Exception as e:
        await send_error(message, e)


@router.message(Command('gdrive_upload'))
async def gdrive_upload_cmd(message: Message, bot: Bot):
    if not is_owner(message.from_user.id):
        return
    try:
        body = message.text.replace('/gdrive_upload', '', 1).strip()
        folder_id = ''
        share_email = ''
        for part in body.split():
            if part.startswith('email='):
                share_email = part.split('=', 1)[1]
            elif part.startswith('folder_id='):
                folder_id = part.split('=', 1)[1]
            elif '@' in part:
                share_email = part
            elif part:
                folder_id = part
        src = await _download_telegram_file(bot, message)
        progress = Progress(message, 'رفع إلى Google Drive')
        await progress.start()
        await progress('☁️ رفع الملف...')
        result = await _drive_client_for(message.from_user.id).upload_file(src, folder_id=folder_id, share_email=share_email)
        await progress('✅ اكتمل الرفع')
        await message.answer(
            '✅ <b>تم رفع الملف إلى Google Drive</b>\n'
            f'Name: <code>{html.escape(result.name)}</code>\n'
            f'ID: <code>{html.escape(result.id)}</code>\n'
            f'Link: {html.escape(result.web_view_link)}\n'
            + (f'Shared with: <code>{html.escape(result.shared_with)}</code>' if result.shared_with else '')
        )
    except Exception as e:
        await send_error(message, e)


@router.message(Command('download_to_gdrive'))
async def download_to_gdrive_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        body = message.text.replace('/download_to_gdrive', '', 1).strip()
        parts = body.split()
        if not parts:
            await message.answer('استخدم: <code>/download_to_gdrive DIRECT_URL [folder_id=...] [email=user@example.com]</code>')
            return
        url = parts[0]
        folder_id = ''
        share_email = ''
        for part in parts[1:]:
            if part.startswith('folder_id='):
                folder_id = part.split('=',1)[1]
            elif part.startswith('email='):
                share_email = part.split('=',1)[1]
            elif '@' in part:
                share_email = part
        progress = Progress(message, 'تحميل ثم رفع إلى Drive')
        await progress.start()
        await progress('⬇️ تحميل الملف...')
        result = await download_direct_url(url, _tmp_user_dir(message.from_user.id), '', allow_html=settings.download_allow_html)
        await progress('☁️ رفع إلى Google Drive...')
        up = await _drive_client_for(message.from_user.id).upload_file(result.path, folder_id=folder_id, share_email=share_email)
        await progress('✅ اكتمل')
        await message.answer(f'✅ تم التحميل والرفع إلى Drive:\n{html.escape(up.web_view_link)}')
    except Exception as e:
        await send_error(message, e)


# -------------------------
# Platform connectors
# -------------------------

def _connector_for(uid: int, platform: str):
    token, meta = store.get_connector_token(uid, platform)
    if not token:
        raise RuntimeError(f'لا يوجد توكن محفوظ لمنصة {platform}. استخدم /connect {platform} TOKEN')
    return build_connector(platform, token, meta)


@router.message(Command('connectors'))
async def connectors_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    items = store.list_connectors(message.from_user.id)
    ai_items = store.list_ai_providers(message.from_user.id)
    lines = ['<b>🌐 الموصلات المتصلة</b>']
    if not items:
        lines.append('لا توجد موصلات. استخدم: <code>/connect railway TOKEN</code> أو <code>/connect vercel TOKEN</code>')
    for item in items:
        lines.append(f"• <b>{html.escape(item.get('platform',''))}</b> — {html.escape(item.get('source','user'))}")
    lines.append('\n<b>🧠 مزودات الذكاء</b>')
    if not ai_items:
        lines.append('لا توجد مفاتيح AI. استخدم: <code>/ai_connect openrouter TOKEN</code>')
    for item in ai_items:
        lines.append(f"• <b>{html.escape(item.get('provider',''))}</b> — {html.escape(item.get('model') or 'default')} — {html.escape(item.get('source','user'))}")
    await message.answer('\n'.join(lines)[:3900])


@router.message(Command('connect'))
async def connect_platform_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        parts = message.text.split(maxsplit=3)
        if len(parts) < 3:
            await message.answer('استخدم: <code>/connect railway RAILWAY_TOKEN</code> أو <code>/connect vercel VERCEL_TOKEN</code>')
            return
        platform, token = parts[1].lower(), parts[2]
        meta = {}
        if len(parts) == 4:
            # optional JSON meta or key=value pairs. Example: team_id=team_x token_kind=project
            rest = parts[3]
            for bit in rest.split():
                if '=' in bit:
                    k, v = bit.split('=', 1)
                    meta[k.strip()] = v.strip()
        store.set_connector_token(message.from_user.id, platform, token, meta)
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(f'✅ تم حفظ موصل <b>{html.escape(platform)}</b> مشفرًا. التوكن: <code>{html.escape(mask_secret(token))}</code>')
    except Exception as e:
        await send_error(message, e)


@router.message(Command('disconnect_connector'))
async def disconnect_connector_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    platform = message.text.replace('/disconnect_connector', '', 1).strip().lower()
    if not platform:
        await message.answer('استخدم: <code>/disconnect_connector railway</code>')
        return
    store.delete_connector_token(message.from_user.id, platform)
    await message.answer(f'✅ تم فصل موصل {html.escape(platform)} لهذا المستخدم.')


@router.message(Command('railway_projects'))
async def railway_projects_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        progress = Progress(message, 'Railway Connector')
        await progress.start()
        await progress('🔐 اختبار التوكن وقراءة المشاريع...')
        connector = _connector_for(message.from_user.id, 'railway')
        result = await connector.projects()
        projects = (result.data or {}).get('projects', [])
        lines = ['<b>🚆 Railway Projects</b>']
        for p in projects[:25]:
            lines.append(f"• <code>{html.escape(p.get('id',''))}</code> — {html.escape(p.get('name',''))}")
        await progress('✅ اكتمل')
        await message.answer('\n'.join(lines)[:3900])
    except Exception as e:
        await send_error(message, e)


@router.message(Command('railway_project'))
async def railway_project_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    project_id = message.text.replace('/railway_project', '', 1).strip()
    if not project_id:
        await message.answer('استخدم: <code>/railway_project PROJECT_ID</code>')
        return
    try:
        result = await _connector_for(message.from_user.id, 'railway').project(project_id)
        project = (result.data or {}).get('project') or {}
        lines = [f"<b>🚆 {html.escape(project.get('name','Railway Project'))}</b>"]
        lines.append('\n<b>Environments</b>')
        for edge in (((project.get('environments') or {}).get('edges')) or []):
            n = edge.get('node') or {}
            lines.append(f"• <code>{html.escape(n.get('id',''))}</code> — {html.escape(n.get('name',''))}")
        lines.append('\n<b>Services</b>')
        for edge in (((project.get('services') or {}).get('edges')) or []):
            n = edge.get('node') or {}
            lines.append(f"• <code>{html.escape(n.get('id',''))}</code> — {html.escape(n.get('name',''))}")
        await message.answer('\n'.join(lines)[:3900])
    except Exception as e:
        await send_error(message, e)


@router.message(Command('railway_set_var'))
async def railway_set_var_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        # /railway_set_var PROJECT_ID ENV_ID SERVICE_ID KEY=VALUE
        parts = message.text.split(maxsplit=4)
        if len(parts) < 5 or '=' not in parts[4]:
            await message.answer('استخدم: <code>/railway_set_var PROJECT_ID ENV_ID SERVICE_ID KEY=VALUE</code>\nاكتب SERVICE_ID = shared للمتغيرات المشتركة.')
            return
        _, project_id, env_id, service_id, pair = parts
        key, value = pair.split('=', 1)
        service = None if service_id.lower() in {'shared', 'none', '-'} else service_id
        progress = Progress(message, 'دفع متغير Railway')
        await progress.start()
        await progress(f'🔐 رفع {html.escape(key)}...')
        result = await _connector_for(message.from_user.id, 'railway').set_variable(project_id, env_id, key, value, service_id=service)
        await progress('✅ اكتمل')
        await message.answer(f'✅ {html.escape(result.message)}')
    except Exception as e:
        await send_error(message, e)


@router.message(Command('railway_set_vars'))
async def railway_set_vars_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        parts = message.text.split(maxsplit=3)
        if len(parts) < 4:
            await message.answer('استخدم وردّ على ملف/رسالة env أو اكتب بعد السطر الأول:\n<code>/railway_set_vars PROJECT_ID ENV_ID SERVICE_ID\nKEY=VALUE</code>')
            return
        _, project_id, env_id, service_id = parts[:4]
        text = message.text.split('\n', 1)[1] if '\n' in message.text else ''
        if message.reply_to_message and message.reply_to_message.text:
            text = message.reply_to_message.text
        variables = parse_env_text(text)
        if not variables:
            await message.answer('لم أجد متغيرات بصيغة KEY=VALUE.')
            return
        service = None if service_id.lower() in {'shared', 'none', '-'} else service_id
        progress = Progress(message, 'دفع متغيرات Railway')
        await progress.start()
        await progress(f'🔐 رفع {len(variables)} متغير...')
        result = await _connector_for(message.from_user.id, 'railway').set_variables(project_id, env_id, variables, service_id=service)
        await progress('✅ اكتمل')
        await message.answer(f'✅ {html.escape(result.message)}')
    except Exception as e:
        await send_error(message, e)


@router.message(Command('vercel_projects'))
async def vercel_projects_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        result = await _connector_for(message.from_user.id, 'vercel').projects()
        projects = (result.data or {}).get('projects', [])
        lines = ['<b>▲ Vercel Projects</b>']
        for p in projects[:30]:
            lines.append(f"• <code>{html.escape(p.get('id',''))}</code> — {html.escape(p.get('name',''))}")
        await message.answer('\n'.join(lines)[:3900])
    except Exception as e:
        await send_error(message, e)


@router.message(Command('vercel_set_var'))
async def vercel_set_var_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        # /vercel_set_var PROJECT production KEY=VALUE
        parts = message.text.split(maxsplit=3)
        if len(parts) < 4 or '=' not in parts[3]:
            await message.answer('استخدم: <code>/vercel_set_var PROJECT production KEY=VALUE</code>')
            return
        _, project, target, pair = parts
        key, value = pair.split('=', 1)
        result = await _connector_for(message.from_user.id, 'vercel').set_variable(project, key, value, target=target)
        await message.answer(f'✅ {html.escape(result.message)}')
    except Exception as e:
        await send_error(message, e)


@router.message(Command('vercel_set_vars'))
async def vercel_set_vars_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            await message.answer('استخدم:\n<code>/vercel_set_vars PROJECT production\nKEY=VALUE</code>')
            return
        _, project, target = parts[:3]
        text = message.text.split('\n', 1)[1] if '\n' in message.text else ''
        if message.reply_to_message and message.reply_to_message.text:
            text = message.reply_to_message.text
        variables = parse_env_text(text)
        if not variables:
            await message.answer('لم أجد متغيرات بصيغة KEY=VALUE.')
            return
        result = await _connector_for(message.from_user.id, 'vercel').set_variables(project, variables, target=target)
        await message.answer(f'✅ {html.escape(result.message)}')
    except Exception as e:
        await send_error(message, e)


# -------------------------
# AI Gateway
# -------------------------

@router.message(Command('ai_connect'))
async def ai_connect_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        # /ai_connect provider TOKEN [base_url] [model]
        parts = message.text.split(maxsplit=4)
        if len(parts) < 3:
            await message.answer('استخدم: <code>/ai_connect openrouter TOKEN</code> أو <code>/ai_connect custom TOKEN https://api.example.com/v1/chat/completions model-name</code>')
            return
        provider = parts[1]
        token = parts[2]
        base_url = parts[3] if len(parts) >= 4 else ''
        model = parts[4] if len(parts) >= 5 else ''
        store.set_ai_token(message.from_user.id, provider, token, base_url, model)
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(f'✅ تم حفظ مزود AI: <b>{html.escape(provider)}</b>')
    except Exception as e:
        await send_error(message, e)


@router.message(Command('ai_status'))
async def ai_status_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    items = store.list_ai_providers(message.from_user.id)
    if not items:
        await message.answer('لا يوجد مزود AI. استخدم /ai_connect')
        return
    lines = ['<b>🧠 AI Providers</b>']
    for item in items:
        lines.append(f"• <code>{html.escape(item.get('provider',''))}</code> model=<code>{html.escape(item.get('model') or 'default')}</code> source={html.escape(item.get('source','user'))}")
    await message.answer('\n'.join(lines))


@router.message(Command('ask_ai'))
async def ask_ai_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        body = message.text.replace('/ask_ai', '', 1).strip()
        if not body:
            await message.answer('استخدم: <code>/ask_ai [provider] سؤالك أو أمر التحليل</code>')
            return
        provider = None
        first, _, rest = body.partition(' ')
        if first.lower() in {'openai','openrouter','gemini','anthropic','groq','mistral','together','perplexity','deepseek','xai','cohere','huggingface','fireworks','custom','lovable','cursor','spiko'} and rest:
            provider = first.lower()
            prompt = rest
        else:
            prompt = body
        provider, token, base_url, model = store.get_ai_token(message.from_user.id, provider)
        gateway = AIGateway(provider, token, base_url, model)
        progress = Progress(message, 'AI Gateway')
        await progress.start()
        await progress(f'🧠 سؤال {provider}...')
        res = await gateway.ask(prompt, system='You are a senior software engineering agent. Be concise, actionable, and safe.')
        await progress('✅ اكتمل')
        await message.answer(f'<b>{html.escape(res.provider)} / {html.escape(res.model)}</b>\n' + html.escape(res.text[:3800]))
    except Exception as e:
        await send_error(message, e)




# -------------------------
# Multistreaming bot section
# -------------------------
def _stream_menu_text(uid: int) -> str:
    status = streaming_manager.status()
    platforms = store.list_stream_platforms(uid)
    lines = [
        '<b>🎥 مركز البث المباشر</b>',
        '',
        f"الحالة: <b>{'نشط ✅' if status.get('active') else 'متوقف ⚫'}</b>",
        f"عدد المنصات المحفوظة: <b>{len(platforms)}</b>",
        '',
        '<b>الأوامر:</b>',
        '• <code>/stream_platform اسم النوع RTMP_URL STREAM_KEY</code>',
        '• <code>/stream_start رابط_يوتيوب_أو_مسار_ملف</code>',
        '• <code>/stream_status</code>',
        '• <code>/stream_stop</code>',
        '',
        'الأنواع: <code>facebook x instagram telegram custom</code>',
    ]
    return '\n'.join(lines)


def _stream_keyboard(active: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text='🎬 بدء بث', callback_data='stream_start_help'), InlineKeyboardButton(text='📊 الحالة', callback_data='stream_status')],
        [InlineKeyboardButton(text='🔑 المنصات', callback_data='stream_platforms'), InlineKeyboardButton(text='➕ إضافة منصة', callback_data='stream_add_help')],
    ]
    if active:
        rows.append([InlineKeyboardButton(text='🛑 إيقاف البث', callback_data='stream_stop')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _platform_url(row: dict) -> str:
    base = str(row.get('rtmp_url') or '').rstrip('/')
    key = str(row.get('stream_key') or '').strip()
    return f'{base}/{key}'


def _select_platforms_keyboard(uid: int, selected: set[int]) -> InlineKeyboardMarkup:
    platforms = store.list_stream_platforms(uid, enabled_only=True)
    rows = []
    for p in platforms:
        pid = int(p['id'])
        mark = '✅' if pid in selected else '⬜'
        rows.append([InlineKeyboardButton(text=f"{mark} {p['platform_type']} · {p['name']}", callback_data=f'stream_toggle:{pid}')])
    rows.append([InlineKeyboardButton(text='🚀 تشغيل الآن', callback_data='stream_confirm'), InlineKeyboardButton(text='❌ إلغاء', callback_data='stream_cancel')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command('stream'))
async def stream_menu_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    await message.answer(_stream_menu_text(message.from_user.id), reply_markup=_stream_keyboard(streaming_manager.is_active()))


@router.message(Command('stream_platform'))
async def stream_platform_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    try:
        rest = message.text.replace('/stream_platform', '', 1).strip()
        parts = rest.split(maxsplit=3)
        if len(parts) < 4:
            await message.answer('استخدم:\n<code>/stream_platform Facebook facebook rtmps://live-api-s.facebook.com:443/rtmp STREAM_KEY</code>')
            return
        name, platform_type, rtmp_url, stream_key = parts
        if not (rtmp_url.startswith('rtmp://') or rtmp_url.startswith('rtmps://')):
            await message.answer('❌ RTMP URL يجب أن يبدأ بـ rtmp:// أو rtmps://')
            return
        store.add_stream_platform(message.from_user.id, name, platform_type, rtmp_url, stream_key)
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(f'✅ تم حفظ منصة البث <b>{html.escape(name)}</b> وتشفير المفتاح محليًا.')
    except Exception as e:
        await send_error(message, e)


@router.message(Command('stream_platforms'))
async def stream_platforms_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    platforms = store.list_stream_platforms(message.from_user.id)
    if not platforms:
        await message.answer('لا توجد منصات محفوظة. استخدم /stream_platform لإضافة وجهة RTMP.')
        return
    lines = ['<b>🔑 منصات البث المحفوظة</b>']
    for p in platforms:
        lines.append(f"#{p['id']} {'✅' if p['enabled'] else '⛔'} <b>{html.escape(p['name'])}</b> · <code>{html.escape(p['platform_type'])}</code> · <code>{html.escape(p['rtmp_url'])}</code>")
    await message.answer('\n'.join(lines)[:3900])


@router.message(Command('stream_start'))
async def stream_start_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    source = message.text.replace('/stream_start', '', 1).strip()
    if not source:
        await message.answer('أرسل المصدر:\n<code>/stream_start https://youtube.com/watch?v=...</code>\nأو\n<code>/stream_start /app/media/video.mp4</code>')
        return
    platforms = store.list_stream_platforms(message.from_user.id, enabled_only=True)
    if not platforms:
        await message.answer('❌ لا توجد منصات مفعلة. أضف منصة أولًا عبر /stream_platform')
        return
    PENDING_STREAMS[message.from_user.id] = {'source': source, 'selected': set()}
    await message.answer('اختر وجهات البث:', reply_markup=_select_platforms_keyboard(message.from_user.id, set()))


@router.message(Command('stream_status'))
async def stream_status_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    status = streaming_manager.status()
    history = store.latest_stream_history(message.from_user.id, limit=3)
    lines = [
        '<b>📊 حالة البث</b>',
        f"نشط: <b>{'نعم ✅' if status.get('active') else 'لا ⚫'}</b>",
        f"الحالة: <code>{html.escape(str(status.get('status', 'offline')))}</code>",
        f"PID: <code>{html.escape(str(status.get('pid') or '-'))}</code>",
        f"الوجهات: <b>{status.get('destinations_count', 0)}</b>",
    ]
    if status.get('error'):
        lines.append(f"خطأ: <code>{html.escape(str(status.get('error'))[:1000])}</code>")
    if history:
        lines.append('\n<b>آخر السجلات:</b>')
        for h in history:
            lines.append(f"#{h['id']} · <code>{html.escape(h['status'])}</code> · {html.escape(h['title'])}")
    await message.answer('\n'.join(lines)[:3900], reply_markup=_stream_keyboard(streaming_manager.is_active()))


@router.message(Command('stream_stop'))
async def stream_stop_cmd(message: Message):
    if not is_owner(message.from_user.id):
        return
    ok = await streaming_manager.stop()
    await message.answer('🛑 تم إرسال أمر إيقاف البث.' if ok else 'لا يوجد بث نشط.', reply_markup=_stream_keyboard(False))


@router.callback_query(F.data == 'stream_menu')
async def stream_menu_cb(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return
    await call.answer()
    await call.message.answer(_stream_menu_text(call.from_user.id), reply_markup=_stream_keyboard(streaming_manager.is_active()))


@router.callback_query(F.data == 'stream_add_help')
async def stream_add_help_cb(call: CallbackQuery):
    await call.answer()
    await call.message.answer('أضف منصة هكذا:\n<code>/stream_platform Facebook facebook rtmps://live-api-s.facebook.com:443/rtmp STREAM_KEY</code>')


@router.callback_query(F.data == 'stream_start_help')
async def stream_start_help_cb(call: CallbackQuery):
    await call.answer()
    await call.message.answer('ابدأ هكذا:\n<code>/stream_start https://youtube.com/watch?v=...</code>\nثم اختر المنصات بالأزرار.')


@router.callback_query(F.data == 'stream_platforms')
async def stream_platforms_cb(call: CallbackQuery):
    await call.answer()
    platforms = store.list_stream_platforms(call.from_user.id)
    if not platforms:
        await call.message.answer('لا توجد منصات محفوظة. استخدم /stream_platform لإضافة وجهة RTMP.')
        return
    lines = ['<b>🔑 منصات البث المحفوظة</b>']
    for p in platforms:
        lines.append(f"#{p['id']} {'✅' if p['enabled'] else '⛔'} <b>{html.escape(p['name'])}</b> · <code>{html.escape(p['platform_type'])}</code> · <code>{html.escape(p['rtmp_url'])}</code>")
    await call.message.answer('\n'.join(lines)[:3900])


@router.callback_query(F.data.startswith('stream_toggle:'))
async def stream_toggle_cb(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return
    await call.answer()
    pid = int(call.data.split(':', 1)[1])
    pending = PENDING_STREAMS.setdefault(call.from_user.id, {'source': '', 'selected': set()})
    selected: set[int] = pending.setdefault('selected', set())
    if pid in selected:
        selected.remove(pid)
    else:
        selected.add(pid)
    await call.message.edit_reply_markup(reply_markup=_select_platforms_keyboard(call.from_user.id, selected))


@router.callback_query(F.data == 'stream_cancel')
async def stream_cancel_cb(call: CallbackQuery):
    PENDING_STREAMS.pop(call.from_user.id, None)
    await call.answer('تم الإلغاء')
    await call.message.answer('تم إلغاء إعداد البث.', reply_markup=_stream_keyboard(streaming_manager.is_active()))


@router.callback_query(F.data == 'stream_confirm')
async def stream_confirm_cb(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return
    await call.answer()
    pending = PENDING_STREAMS.get(call.from_user.id) or {}
    source = pending.get('source') or ''
    selected = list(pending.get('selected') or [])
    if not source:
        await call.message.answer('❌ لا يوجد مصدر محفوظ. استخدم /stream_start من جديد.')
        return
    if not selected:
        await call.message.answer('❌ اختر منصة واحدة على الأقل.')
        return
    rows = store.get_stream_platforms_by_ids(call.from_user.id, [int(x) for x in selected])
    if not rows:
        await call.message.answer('❌ لم أجد منصات مفعلة صالحة.')
        return
    destinations = [_platform_url(r) for r in rows]
    title = f'Telegram multistream {time_now_short()}'
    hist_id = store.create_stream_history(
        telegram_id=call.from_user.id,
        title=title,
        source=source,
        source_type='youtube' if ('youtube.com/' in source or 'youtu.be/' in source) else 'file_or_direct',
        status='starting',
        destinations=[{'id': r['id'], 'name': r['name'], 'type': r['platform_type']} for r in rows],
    )
    msg = await call.message.answer('⏳ جاري تشغيل FFmpeg وتجهيز البث...')
    try:
        session = await streaming_manager.start(source=source, destinations=destinations, title=title, prefer_copy=True)
        async def on_stream_event(event: str, payload: dict) -> None:
            if event == 'active':
                store.update_stream_history(hist_id, status='active', pid=payload.get('pid'))
                try:
                    await msg.edit_text(f"✅ بدأ البث بنجاح.\nPID: <code>{payload.get('pid')}</code>", reply_markup=_stream_keyboard(True))
                except Exception:
                    pass
            elif event == 'error':
                store.update_stream_history(hist_id, status='failed', error=str(payload.get('error') or ''), ended=True)
                await call.message.answer('❌ فشل البث:\n<code>' + html.escape(str(payload.get('error') or '')[:1200]) + '</code>', reply_markup=_stream_keyboard(False))
            elif event == 'end':
                store.update_stream_history(hist_id, status='completed', ended=True)
        session.on(on_stream_event)
        PENDING_STREAMS.pop(call.from_user.id, None)
    except Exception as e:
        store.update_stream_history(hist_id, status='failed', error=str(e), ended=True)
        await msg.edit_text('❌ فشل تشغيل البث:\n<code>' + html.escape(str(e)[:1200]) + '</code>', reply_markup=_stream_keyboard(False))


@router.callback_query(F.data == 'stream_status')
async def stream_status_cb(call: CallbackQuery):
    await call.answer()
    status = streaming_manager.status()
    history = store.latest_stream_history(call.from_user.id, limit=3)
    lines = ['<b>📊 حالة البث</b>', f"نشط: <b>{'نعم ✅' if status.get('active') else 'لا ⚫'}</b>", f"الحالة: <code>{html.escape(str(status.get('status', 'offline')))}</code>"]
    if history:
        lines.append('\n<b>آخر السجلات:</b>')
        for h in history:
            lines.append(f"#{h['id']} · <code>{html.escape(h['status'])}</code> · {html.escape(h['title'])}")
    await call.message.answer('\n'.join(lines)[:3900], reply_markup=_stream_keyboard(streaming_manager.is_active()))


@router.callback_query(F.data == 'stream_stop')
async def stream_stop_cb(call: CallbackQuery):
    await call.answer()
    ok = await streaming_manager.stop()
    await call.message.answer('🛑 تم إرسال أمر إيقاف البث.' if ok else 'لا يوجد بث نشط.', reply_markup=_stream_keyboard(False))


def time_now_short() -> str:
    import datetime
    return datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')


@router.callback_query(F.data.startswith('help_'))
async def help_callback(call: CallbackQuery):
    texts = {
        'help_repo': 'ربط مستودع:\n<code>/repo https://github.com/OWNER/REPO</code>',
        'help_token': 'ربط توكن:\n<code>/token github_pat_xxx</code>\nالأفضل وضعه في Railway إذا البوت خاص بك فقط.',
        'help_create_repo': 'إنشاء مستودع:\n<code>/create_repo my-project private</code> أو <code>/create_repo my-project public</code>',
        'help_branch': 'إنشاء فرع:\n<code>/new_branch feature-name</code>\nتغيير الفرع:\n<code>/branch main</code>',
        'help_unpack': 'أرسل ملف zip/rar/7z/tar ثم رد عليه:\n<code>/unpack</code>\nيرفع المشروع المرتب إلى جذر المستودع تلقائيًا. لو تريد حفظ مجلد فرعي استخدم: <code>/unpack target/folder --keep-folder</code>.',
        'help_replace': 'استبدال كامل مرتب للمستودع: أرسل zip/rar/7z/tar ثم رد عليه: <code>/replace</code> أو <code>/replace https://github.com/OWNER/REPO --force</code>. يدعم: <code>--dry-run</code> و <code>--keep README.md .env.example</code> و <code>--target apps/web</code> و <code>--no-delete</code>.',
        'help_normalize': 'لترتيب مشروع فقط وإرسال ZIP نظيف بدون رفع إلى GitHub:\nرد على الملف بالأمر <code>/normalize</code>',
        'help_upload': 'أرسل ملفًا ثم رد عليه:\n<code>/upload path/in/repo.ext</code>',
        'help_supabase': 'قراءة جدول مصرح به:\n<code>/supabase posts 10</code>',
        'help_commands': '<code>/connections /current_repo /switch_repo /disconnect_repo /disconnect_all /info /repos /ls /read /write /delete /upload /unpack /replace /normalize /create_repo /new_branch /pr /supabase /agent /plan /task /approve_plan /task_status /task_logs /index_repo /memory_status /fix_last_error /autofix /sandbox_run /term /analyze_repo /install_workflow /codespace /connectors /connect /railway_projects /railway_set_vars /vercel_projects /ai_connect /ask_ai /download_file /apk /download_to_repo /gdrive_connect /gdrive_upload /download_to_gdrive</code>',
        'help_agent': 'أوامر Agent:\n<code>/agent https://github.com/OWNER/REPO\nreplace app/main.py\nالمحتوى</code>\n<code>/agent ...\nmkdir app/new</code>\n<code>/analyze_repo https://github.com/OWNER/REPO</code>',
        'help_terminal': 'الطرفية تعمل عبر GitHub Actions بعد تثبيت Workflow:\n<code>/install_workflow https://github.com/OWNER/REPO</code>\nثم:\n<code>/term https://github.com/OWNER/REPO\nnpm run build</code>',
        'help_connectors': 'الموصلات:\n<code>/connect railway TOKEN</code>\n<code>/railway_projects</code>\n<code>/railway_project PROJECT_ID</code>\n<code>/railway_set_var PROJECT_ID ENV_ID SERVICE_ID KEY=VALUE</code>\n<code>/railway_set_vars PROJECT_ID ENV_ID SERVICE_ID</code> ثم ضع env في السطور التالية.\nVercel: <code>/connect vercel TOKEN</code> ثم <code>/vercel_projects</code> و <code>/vercel_set_var PROJECT production KEY=VALUE</code>',
        'help_ai': 'AI Gateway:\n<code>/ai_connect openrouter TOKEN</code>\n<code>/ai_connect openai TOKEN</code>\n<code>/ai_connect gemini TOKEN</code>\n<code>/ask_ai openrouter حلل هذا الخطأ...</code>\nيدعم custom OpenAI-compatible: <code>/ai_connect custom TOKEN https://api.example.com/v1/chat/completions model</code>',
    }
    await call.message.answer(texts.get(call.data, 'غير معروف'))
    await call.answer()


@router.callback_query(F.data == 'cmd_info')
async def cb_info(call: CallbackQuery):
    try:
        client, user = get_client_for(call.from_user.id)
        viewer = await client.viewer()
        token_status = 'خاص بالمستخدم' if user.get('github_token') else 'من Railway GITHUB_TOKEN'
        await call.message.answer(
            f"👤 GitHub: <b>{html.escape(viewer.get('login',''))}</b>\n"
            f"🔐 التوكن: {token_status}\n"
            f"📦 Repo: <code>{html.escape(user.get('repo') or 'غير محدد')}</code>\n"
            f"🌿 Branch: <code>{html.escape(user.get('branch') or settings.github_default_branch)}</code>"
        )
    except Exception as e:
        await call.message.answer('❌ ' + html.escape(str(e))[:3500])
    await call.answer()


@router.callback_query(F.data == 'cmd_ls')
async def cb_ls(call: CallbackQuery):
    # fake minimal command context: use sender id from callback
    try:
        client, _, owner, repo, branch = repo_context(call.from_user.id)
        data = await client.list_contents(owner, repo, '', branch)
        lines = ['<b>جذر المستودع:</b>']
        for item in data[:60]:
            icon = '📁' if item.get('type') == 'dir' else '📄'
            lines.append(f"{icon} <code>{html.escape(item.get('path',''))}</code>")
        await call.message.answer('\n'.join(lines)[:4000])
    except Exception as e:
        await call.message.answer('❌ ' + html.escape(str(e))[:3500])
    await call.answer()


@router.callback_query(F.data == 'cmd_connections')
async def cb_connections(call: CallbackQuery):
    try:
        await call.message.answer(await _connections_text(call.from_user.id))
    except Exception as e:
        await call.message.answer('❌ ' + html.escape(str(e))[:3500])
    await call.answer()


@router.callback_query(F.data == 'cmd_current_repo')
async def cb_current_repo(call: CallbackQuery):
    try:
        session = store.get_session(call.from_user.id)
        if not session:
            await call.message.answer('لا يوجد مستودع حالي. استخدم /switch_repo رابط_المستودع')
        else:
            await call.message.answer(
                '📌 <b>المستودع الحالي</b>\n'
                f"Repo: <code>{html.escape(session.get('repo_url',''))}</code>\n"
                f"Branch: <code>{html.escape(session.get('branch') or settings.github_default_branch)}</code>\n"
                f"Token: <code>{html.escape(session.get('github_token_id',''))}</code>"
            )
    except Exception as e:
        await call.message.answer('❌ ' + html.escape(str(e))[:3500])
    await call.answer()


@router.callback_query(F.data.startswith('approve_plan:'))
async def cb_approve_plan(call: CallbackQuery):
    try:
        task_id = call.data.split(':', 1)[1]
        fake = call.message
        # call.message lacks from_user; create a tiny adapter for functions expecting Message-like object.
        class _Adapter:
            def __init__(self, msg, user):
                self._msg = msg
                self.from_user = user
                self.chat = msg.chat
                self.text = ''
            async def answer(self, *args, **kwargs):
                return await self._msg.answer(*args, **kwargs)
        await _execute_pending_plan(_Adapter(call.message, call.from_user), task_id)
    except Exception as e:
        await call.message.answer('❌ ' + html.escape(str(e))[:3500])
    await call.answer()


@router.callback_query(F.data.startswith('cancel_task:'))
async def cb_cancel_task(call: CallbackQuery):
    task_id = call.data.split(':', 1)[1]
    try:
        store.update_task(task_id, 'cancelled')
        PENDING_PLANS.pop(call.from_user.id, None)
        await call.message.answer(f'✅ تم إلغاء المهمة <code>{html.escape(task_id)}</code>')
    except Exception as e:
        await call.message.answer('❌ ' + html.escape(str(e))[:3500])
    await call.answer()


@router.callback_query(F.data == 'cmd_task_logs')
async def cb_task_logs(call: CallbackQuery):
    rows = store.task_logs(call.from_user.id, None, 20)
    if not rows:
        await call.message.answer('لا توجد سجلات بعد.')
    else:
        lines = ['<b>سجلات Agent:</b>']
        for r in rows[:15]:
            lines.append(f"• <code>{html.escape(r.get('step') or '')}</code> {html.escape((r.get('message') or '')[:220])}")
        await call.message.answer('\n'.join(lines)[:3900])
    await call.answer()


def build_bot() -> Bot:
    return Bot(token=settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp
