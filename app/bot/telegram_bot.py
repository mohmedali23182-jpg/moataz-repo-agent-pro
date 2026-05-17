from __future__ import annotations

import asyncio
import html
import json
import shutil
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.client.default import DefaultBotProperties

from app.config import get_settings
from app.services.archive import extract_archive
from app.services.github_client import GitHubClient, GitHubAuth, GitHubError, parse_repo, token_for_user, GitHubAppAuth
from app.services.store import Store
from app.services.supabase_client import SupabaseClient

router = Router()
store = Store()
settings = get_settings()


def is_owner(user_id: int) -> bool:
    return not settings.owner_ids or user_id in settings.owner_ids


def menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text='🔗 ربط مستودع', callback_data='help_repo'), InlineKeyboardButton(text='🔑 ربط توكن', callback_data='help_token')],
        [InlineKeyboardButton(text='📂 الملفات', callback_data='cmd_ls'), InlineKeyboardButton(text='👤 الحساب', callback_data='cmd_info')],
        [InlineKeyboardButton(text='🆕 إنشاء Repo', callback_data='help_create_repo'), InlineKeyboardButton(text='🌿 إنشاء Branch', callback_data='help_branch')],
        [InlineKeyboardButton(text='📦 فك ضغط ورفع', callback_data='help_unpack'), InlineKeyboardButton(text='⬆️ رفع ملف', callback_data='help_upload')],
        [InlineKeyboardButton(text='🧠 Supabase', callback_data='help_supabase'), InlineKeyboardButton(text='🧾 الأوامر', callback_data='help_commands')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def help_text() -> str:
    return '''<b>Moataz Repo Agent</b>
بوت فعلي لإدارة GitHub حسب الصلاحيات التي تعطيها له.

<b>ابدأ هكذا:</b>
1) /token github_pat_xxx
2) /repo https://github.com/OWNER/REPO
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
async def set_repo(message: Message):
    repo = message.text.replace('/repo', '', 1).strip()
    if not repo:
        await message.answer('استخدم: <code>/repo https://github.com/OWNER/REPO</code>')
        return
    parse_repo(repo)
    store.set_repo(message.from_user.id, repo)
    await message.answer(f'✅ تم ربط المستودع: <code>{html.escape(repo)}</code>')


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
        client, _ = get_client_for(message)
        repo = await client.create_repo(name=name, private=private, description='Created by Moataz Repo Agent')
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


@router.message(Command('unpack'))
async def unpack(message: Message, bot: Bot):
    try:
        target_dir = message.text.replace('/unpack', '', 1).strip().strip('/') or 'uploaded_archive'
        src = await _download_telegram_file(bot, message)
        extract_dir = src.parent / (src.stem + '_extracted')
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        files = extract_archive(src, extract_dir)
        client, _, owner, repo, branch = repo_context(message.from_user.id)
        await message.answer(f'📦 تم فك الضغط. جاري رفع {len(files)} ملف...')
        uploaded = 0
        for p in files:
            rel = p.relative_to(extract_dir).as_posix()
            gh_path = f'{target_dir}/{rel}'.replace('//', '/')
            await client.put_file(owner, repo, gh_path, p.read_bytes(), branch, f'Upload extracted {gh_path}')
            uploaded += 1
            if uploaded % 10 == 0:
                await message.answer(f'⬆️ تم رفع {uploaded}/{len(files)}')
        await message.answer(f'✅ اكتمل فك الضغط والرفع: {uploaded} ملف إلى <code>{html.escape(target_dir)}</code>')
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


@router.callback_query(F.data.startswith('help_'))
async def help_callback(call: CallbackQuery):
    texts = {
        'help_repo': 'ربط مستودع:\n<code>/repo https://github.com/OWNER/REPO</code>',
        'help_token': 'ربط توكن:\n<code>/token github_pat_xxx</code>\nالأفضل وضعه في Railway إذا البوت خاص بك فقط.',
        'help_create_repo': 'إنشاء مستودع:\n<code>/create_repo my-project private</code> أو <code>/create_repo my-project public</code>',
        'help_branch': 'إنشاء فرع:\n<code>/new_branch feature-name</code>\nتغيير الفرع:\n<code>/branch main</code>',
        'help_unpack': 'أرسل ملف zip/rar/7z/tar ثم رد عليه:\n<code>/unpack target/folder</code>',
        'help_upload': 'أرسل ملفًا ثم رد عليه:\n<code>/upload path/in/repo.ext</code>',
        'help_supabase': 'قراءة جدول مصرح به:\n<code>/supabase posts 10</code>',
        'help_commands': '<code>/info /repos /ls /read /write /delete /upload /unpack /create_repo /new_branch /pr /supabase</code>',
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


def build_bot() -> Bot:
    return Bot(token=settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp
