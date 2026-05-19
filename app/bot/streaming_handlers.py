from __future__ import annotations

import html
import json
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from app.services.store import Store
from app.services.streaming import streaming_manager
from app.config import get_settings

router = Router()
store = Store()
settings = get_settings()

class StreamStates(StatesGroup):
    waiting_for_channel_username = State()
    waiting_for_rtmp_data = State()
    waiting_for_source = State()
    selecting_channels = State()

def is_owner(user_id: int) -> bool:
    return not settings.owner_ids or user_id in settings.owner_ids

def get_stream_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text='▶️ بدء بث', callback_data='stream_start_flow'), InlineKeyboardButton(text='🎧 بث صوت', callback_data='stream_audio_flow')],
        [InlineKeyboardButton(text='📡 قنوات البث', callback_data='stream_channels_list')],
        [InlineKeyboardButton(text='📊 حالة البث', callback_data='stream_status_info'), InlineKeyboardButton(text='🛑 إيقاف البث', callback_data='stream_stop_confirm')],
        [InlineKeyboardButton(text='🔙 القائمة الرئيسية', callback_data='show_menu')]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

@router.callback_query(F.data == 'stream_menu')
async def stream_main_menu(callback: CallbackQuery):
    if not is_owner(callback.from_user.id): return
    await callback.message.edit_text('🎥 <b>قسم البث المباشر</b>\nإدارة البث إلى قنوات تليجرام أو منصات أخرى عبر RTMP.', reply_markup=get_stream_menu())

@router.message(Command('stream_channels'))
@router.callback_query(F.data == 'stream_channels_list')
async def list_channels(event: Message | CallbackQuery):
    uid = event.from_user.id
    if not is_owner(uid): return
    
    channels = store.list_stream_channels(uid)
    if not channels:
        text = "❌ لا توجد قنوات مسجلة حالياً."
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='➕ إضافة قناة', callback_data='stream_add_channel_start')]])
    else:
        text = "<b>📡 قنوات البث المسجلة:</b>\n\n"
        for c in channels:
            status = '✅' if c['enabled'] else '⬜'
            text += f"{status} <b>{html.escape(c['title'] or 'بدون عنوان')}</b>\n"
            text += f"└ <code>{html.escape(c['username'] or c['chat_id'])}</code>\n"
            text += f"└ RTMP: <code>{html.escape(c['rtmp_url'] or 'غير مضبوط')}</code>\n\n"
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='➕ إضافة قناة', callback_data='stream_add_channel_start')],
            [InlineKeyboardButton(text='🔑 ضبط RTMP', callback_data='stream_set_rtmp_start')],
            [InlineKeyboardButton(text='🗑️ حذف قناة', callback_data='stream_remove_channel_start')],
            [InlineKeyboardButton(text='🔙 رجوع', callback_data='stream_menu')]
        ])
    
    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb)
    else:
        await event.message.edit_text(text, reply_markup=kb)

@router.callback_query(F.data == 'stream_add_channel_start')
async def add_channel_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(StreamStates.waiting_for_channel_username)
    await callback.message.edit_text(
        "➕ <b>إضافة قناة جديدة</b>\n\n"
        "من فضلك أرسل معرف القناة (username) مثل <code>@channel_name</code>\n"
        "أو قم بإعادة توجيه (Forward) رسالة من القناة إلى هنا.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='❌ إلغاء', callback_data='stream_menu')]])
    )

@router.message(StreamStates.waiting_for_channel_username)
async def process_channel_input(message: Message, state: FSMContext):
    chat_id = None
    username = None
    title = None
    
    if message.forward_from_chat:
        chat_id = message.forward_from_chat.id
        username = message.forward_from_chat.username
        title = message.forward_from_chat.title
    elif message.text and message.text.startswith('@'):
        username = message.text.strip()
        try:
            chat = await message.bot.get_chat(username)
            chat_id = chat.id
            title = chat.title
        except Exception as e:
            await message.answer(f"❌ تعذر العثور على القناة: {str(e)}")
            return
    else:
        await message.answer("❌ يرجى إرسال معرف صحيح يبدأ بـ @ أو إعادة توجيه رسالة.")
        return

    store.add_stream_channel(message.from_user.id, str(chat_id), username, title)
    await state.clear()
    await message.answer(f"✅ تم تسجيل القناة: <b>{html.escape(title)}</b>\nالآن قم بضبط بيانات RTMP الخاصة بها.", reply_markup=get_stream_menu())

@router.callback_query(F.data == 'stream_set_rtmp_start')
async def set_rtmp_start(callback: CallbackQuery, state: FSMContext):
    channels = store.list_stream_channels(callback.from_user.id)
    if not channels:
        await callback.answer("لا توجد قنوات لتعديلها.")
        return
    
    buttons = []
    for c in channels:
        buttons.append([InlineKeyboardButton(text=c['title'], callback_data=f"set_rtmp_id:{c['chat_id']}")])
    
    buttons.append([InlineKeyboardButton(text='🔙 رجوع', callback_data='stream_channels_list')])
    await callback.message.edit_text("اختر القناة لضبط بيانات RTMP:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith('set_rtmp_id:'))
async def set_rtmp_details(callback: CallbackQuery, state: FSMContext):
    chat_id = callback.data.split(':')[1]
    await state.update_data(target_chat_id=chat_id)
    await state.set_state(StreamStates.waiting_for_rtmp_data)
    await callback.message.edit_text(
        "🔑 <b>ضبط بيانات RTMP</b>\n\n"
        "أرسل البيانات بالتنسيق التالي:\n"
        "<code>RTMP_URL</code>\n"
        "<code>STREAM_KEY</code>\n\n"
        "مثال:\n"
        "<code>rtmp://localhost/live</code>\n"
        "<code>1234-5678-abcd</code>"
    )

@router.message(StreamStates.waiting_for_rtmp_data)
async def process_rtmp_data(message: Message, state: FSMContext):
    data = await state.get_data()
    chat_id = data.get('target_chat_id')
    lines = message.text.strip().splitlines()
    
    if len(lines) < 2:
        await message.answer("❌ يرجى إرسال الرابط في السطر الأول ومفتاح البث في السطر الثاني.")
        return
    
    rtmp_url = lines[0].strip()
    stream_key = lines[1].strip()
    
    store.update_stream_channel_rtmp(message.from_user.id, chat_id, rtmp_url, stream_key)
    await state.clear()
    await message.answer("✅ تم تحديث بيانات RTMP بنجاح.", reply_markup=get_stream_menu())

@router.callback_query(F.data == 'stream_start_flow')
async def stream_start_flow(callback: CallbackQuery, state: FSMContext):
    if streaming_manager.is_active():
        await callback.answer("يوجد بث نشط بالفعل!", show_alert=True)
        return
    
    await state.set_state(StreamStates.waiting_for_source)
    await callback.message.edit_text(
        "▶️ <b>بدء بث جديد</b>\n\n"
        "أرسل رابط YouTube أو مسار ملف فيديو محلي.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='❌ إلغاء', callback_data='stream_menu')]])
    )

@router.message(StreamStates.waiting_for_source)
async def process_stream_source(message: Message, state: FSMContext):
    source = message.text.strip()
    await state.update_data(stream_source=source)
    
    channels = store.list_stream_channels(message.from_user.id, only_enabled=True)
    if not channels:
        await message.answer("❌ لا توجد قنوات مفعلة للبث إليها. أضف قناة أولاً.")
        await state.clear()
        return

    await state.set_state(StreamStates.selecting_channels)
    await state.update_data(selected_channels=[])
    await show_channel_selection(message, channels, [])

async def show_channel_selection(message: Message, channels: list, selected: list):
    text = "🚀 <b>اختر القنوات للبث إليها:</b>"
    buttons = []
    for c in channels:
        mark = '✅' if c['chat_id'] in selected else '⬜'
        buttons.append([InlineKeyboardButton(text=f"{mark} {c['title']}", callback_data=f"toggle_chan:{c['chat_id']}")])
    
    buttons.append([InlineKeyboardButton(text='🚀 بدء البث الآن', callback_data='stream_execute_start')])
    buttons.append([InlineKeyboardButton(text='❌ إلغاء', callback_data='stream_menu')])
    
    if isinstance(message, Message):
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    else:
        await message.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith('toggle_chan:'))
async def toggle_channel(callback: CallbackQuery, state: FSMContext):
    chat_id = callback.data.split(':')[1]
    data = await state.get_data()
    selected = data.get('selected_channels', [])
    
    if chat_id in selected:
        selected.remove(chat_id)
    else:
        selected.append(chat_id)
    
    await state.update_data(selected_channels=selected)
    channels = store.list_stream_channels(callback.from_user.id, only_enabled=True)
    await show_channel_selection(callback, channels, selected)

@router.callback_query(F.data == 'stream_execute_start')
async def execute_stream_start(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    source = data.get('stream_source')
    selected_ids = data.get('selected_channels', [])
    
    if not selected_ids:
        await callback.answer("يرجى اختيار قناة واحدة على الأقل!", show_alert=True)
        return
    
    all_channels = store.list_stream_channels(callback.from_user.id)
    destinations = []
    channel_titles = []
    
    for c in all_channels:
        if c['chat_id'] in selected_ids:
            if not c['rtmp_url'] or not c['stream_key']:
                await callback.answer(f"القناة {c['title']} تفتقد لبيانات RTMP!", show_alert=True)
                return
            full_url = c['rtmp_url']
            if not full_url.endswith('/'): full_url += '/'
            destinations.append(f"{full_url}{c['stream_key']}")
            channel_titles.append(c['title'])

    await callback.message.edit_text("⏳ جاري تحضير البث المباشر...")
    
    try:
        session = await streaming_manager.start(
            source=source,
            destinations=destinations,
            title=f"بث إلى: {', '.join(channel_titles)}"
        )
        
        async def stream_log_callback(event, payload):
            if event == "active":
                await callback.message.answer(f"✅ <b>بدأ البث بنجاح!</b>\nالمصدر: <code>{html.escape(source)}</code>\nPID: <code>{payload['pid']}</code>")
            elif event == "error":
                await callback.message.answer(f"❌ <b>خطأ في البث:</b>\n<code>{html.escape(payload['error'])}</code>")
        
        session.on(stream_log_callback)
        await state.clear()
        
    except Exception as e:
        await callback.message.edit_text(f"❌ فشل بدء البث: {str(e)}")

@router.callback_query(F.data == 'stream_status_info')
async def stream_status_info(callback: CallbackQuery):
    status = streaming_manager.status()
    if not status['active']:
        await callback.message.edit_text("📊 <b>حالة البث:</b> متوقف (Offline)", reply_markup=get_stream_menu())
        return
    
    text = (
        "📊 <b>حالة البث الحالية:</b>\n\n"
        f"📍 الحالة: <code>{status['status']}</code>\n"
        f"📺 المصدر: <code>{html.escape(status['source'])}</code>\n"
        f"📡 القنوات: <code>{status['destinations_count']}</code>\n"
        f"⏱️ البدء: <code>{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(status['started_at']))}</code>\n"
    )
    if status.get('error'):
        text += f"⚠️ خطأ: <code>{html.escape(status['error'])}</code>"
    
    await callback.message.edit_text(text, reply_markup=get_stream_menu())

@router.callback_query(F.data == 'stream_stop_confirm')
async def stream_stop_confirm(callback: CallbackQuery):
    if not streaming_manager.is_active():
        await callback.answer("لا يوجد بث نشط لإيقافه.")
        return
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='🛑 نعم، أوقف البث', callback_data='stream_stop_execute')],
        [InlineKeyboardButton(text='❌ إلغاء', callback_data='stream_menu')]
    ])
    await callback.message.edit_text("هل أنت متأكد من رغبتك في إيقاف البث المباشر الحالي؟", reply_markup=kb)

@router.callback_query(F.data == 'stream_stop_execute')
async def stream_stop_execute(callback: CallbackQuery):
    stopped = await streaming_manager.stop()
    if stopped:
        await callback.message.edit_text("✅ تم إرسال أمر الإيقاف بنجاح.", reply_markup=get_stream_menu())
    else:
        await callback.message.edit_text("❌ لم يتم العثور على بث نشط.", reply_markup=get_stream_menu())
