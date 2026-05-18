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

router = Router()
store = Store()
settings = get_settings()
PENDING_TERMINAL: dict[int, dict] = {}


def is_owner(user_id: int) -> bool:
    return not settings.owner_ids or user_id in settings.owner_ids


def menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text='🔗 ربط مستودع', callback_data='help_repo'), InlineKeyboardButton(text='🔑 ربط توكن', callback_data='help_token')],
        [InlineKeyboardButton(text='📂 الملفات', callback_data='cmd_ls'), InlineKeyboardButton(text='👤 الحساب', callback_data='cmd_info')],
        [InlineKeyboardButton(text='🔌 الاتصالات', callback_data='cmd_connections'), InlineKeyboardButton(text='📌 الريبو الحالي', callback_data='cmd_current_repo')],
        [InlineKeyboardButton(text='🆕 إنشاء Repo', callback_data='help_create_repo'), InlineKeyboardButton(text='🌿 إنشاء Branch', callback_data='help_branch')],
        [InlineKeyboardButton(text='📦 فك ضغط ورفع', callback_data='help_unpack'), InlineKeyboardButton(text='🚀 ترتيب مشروع', callback_data='help_normalize')],
        [InlineKeyboardButton(text='⬆️ رفع ملف', callback_data='help_upload')],
        [InlineKeyboardButton(text='🧠 Supabase', callback_data='help_supabase'), InlineKeyboardButton(text='🧾 الأوامر', callback_data='help_commands')],
        [InlineKeyboardButton(text='🤖 Agent', callback_data='help_agent'), InlineKeyboardButton(text='💻 Terminal', callback_data='help_terminal')],
        [InlineKeyboardButton(text='🌐 Connectors', callback_data='help_connectors'), InlineKeyboardButton(text='🧠 AI Gateway', callback_data='help_ai')],
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
        if first.lower() in {'openai','openrouter','gemini','custom','lovable','cursor','spiko'} and rest:
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


@router.callback_query(F.data.startswith('help_'))
async def help_callback(call: CallbackQuery):
    texts = {
        'help_repo': 'ربط مستودع:\n<code>/repo https://github.com/OWNER/REPO</code>',
        'help_token': 'ربط توكن:\n<code>/token github_pat_xxx</code>\nالأفضل وضعه في Railway إذا البوت خاص بك فقط.',
        'help_create_repo': 'إنشاء مستودع:\n<code>/create_repo my-project private</code> أو <code>/create_repo my-project public</code>',
        'help_branch': 'إنشاء فرع:\n<code>/new_branch feature-name</code>\nتغيير الفرع:\n<code>/branch main</code>',
        'help_unpack': 'أرسل ملف zip/rar/7z/tar ثم رد عليه:\n<code>/unpack</code>\nيرفع المشروع المرتب إلى جذر المستودع تلقائيًا. لو تريد حفظ مجلد فرعي استخدم: <code>/unpack target/folder --keep-folder</code>.',
        'help_normalize': 'لترتيب مشروع فقط وإرسال ZIP نظيف بدون رفع إلى GitHub:\nرد على الملف بالأمر <code>/normalize</code>',
        'help_upload': 'أرسل ملفًا ثم رد عليه:\n<code>/upload path/in/repo.ext</code>',
        'help_supabase': 'قراءة جدول مصرح به:\n<code>/supabase posts 10</code>',
        'help_commands': '<code>/connections /current_repo /switch_repo /disconnect_repo /disconnect_all /info /repos /ls /read /write /delete /upload /unpack /normalize /create_repo /new_branch /pr /supabase /agent /term /analyze_repo /install_workflow /codespace /connectors /connect /railway_projects /railway_set_vars /vercel_projects /ai_connect /ask_ai</code>',
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


def build_bot() -> Bot:
    return Bot(token=settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp
