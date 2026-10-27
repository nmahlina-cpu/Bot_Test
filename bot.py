"""
Telegram-бот для Дарины — воронка продаж через тестирование.

Функционал:
- Приветственное видео
- Тест «Играешь роль или живёшь?» (10 вопросов, A/B/C/D)
- Проверка подписки на канал @darina_v_smisle
- 4 варианта результатов с видео-подкастами
- Воронка: интенсив (типы 1-3) или консультация (тип 4)
- Обработка возражений для отказавшихся
"""

import asyncio
import logging
import sqlite3
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from telegram.error import TelegramError
from telegram.request import HTTPXRequest

from config import (
    BOT_TOKEN, CHANNEL_USERNAME, ADMIN_ID, ADMIN_CONTACT,
    DB_PATH, QUESTIONS, RESULTS, MESSAGES,
    OBJECTION_QUESTIONS,
    WELCOME_VIDEO, WELCOME_VIDEO_AS_NOTE,
    VIDEO_RESULT_A, VIDEO_RESULT_B,
    VIDEO_RESULT_C, VIDEO_RESULT_D, VIDEO_CONSULTATION,
)

# ============ ЛОГИРОВАНИЕ ============
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

import os

# Словарь video file_id по типу результата
RESULT_VIDEOS = {
    "A": VIDEO_RESULT_A,
    "B": VIDEO_RESULT_B,
    "C": VIDEO_RESULT_C,
    "D": VIDEO_RESULT_D,
}

# Словарь подкастов (mp3) по типу результата
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULT_PODCASTS = {
    "A": os.path.join(BASE_DIR, "podcasts", "1.mp3"),
    "B": os.path.join(BASE_DIR, "podcasts", "2.mp3"),
    "C": os.path.join(BASE_DIR, "podcasts", "3.mp3"),
    "D": os.path.join(BASE_DIR, "podcasts", "4.mp3"),
}

PODCAST_TITLES = {
    "A": "🎧 Подкаст: Ключи к проявленности — Тип 1",
    "B": "🎧 Подкаст: Ключи нового образа — Тип 2",
    "C": "🎧 Подкаст: Ключи нового уровня — Тип 3",
    "D": "🎧 Подкаст: Ключи масштаба — Тип 4",
}


# ============ БАЗА ДАННЫХ ============
class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.init_db()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def init_db(self):
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                result_type TEXT,
                answers TEXT,
                intensive_response TEXT,
                objection_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()
        logger.info("✅ База данных инициализирована")

    def save_user(self, user_id: int, username: str, first_name: str, last_name: str):
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO users (user_id, username, first_name, last_name)
            VALUES (?, ?, ?, ?)
        ''', (user_id, username, first_name, last_name))
        conn.commit()
        conn.close()

    def save_result(self, user_id: int, result_type: str, answers: str):
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE users SET result_type = ?, answers = ?, finished_at = ?
            WHERE user_id = ?
        ''', (result_type, answers, datetime.now(), user_id))
        conn.commit()
        conn.close()

    def save_intensive_response(self, user_id: int, response: str):
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE users SET intensive_response = ? WHERE user_id = ?
        ''', (response, user_id))
        conn.commit()
        conn.close()

    def save_objection(self, user_id: int, reason: str):
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE users SET objection_reason = ? WHERE user_id = ?
        ''', (reason, user_id))
        conn.commit()
        conn.close()

    def get_stats(self):
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM users')
        total = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM users WHERE result_type IS NOT NULL')
        finished = cursor.fetchone()[0]
        cursor.execute('SELECT result_type, COUNT(*) FROM users WHERE result_type IS NOT NULL GROUP BY result_type')
        by_type = dict(cursor.fetchall())
        cursor.execute("SELECT COUNT(*) FROM users WHERE intensive_response = 'yes'")
        intensive_yes = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users WHERE intensive_response = 'no'")
        intensive_no = cursor.fetchone()[0]
        conn.close()
        return {
            'total': total,
            'finished': finished,
            'by_type': by_type,
            'intensive_yes': intensive_yes,
            'intensive_no': intensive_no,
        }


db = Database()


# ============ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ============
async def check_subscription(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверяет подписку пользователя на канал."""
    try:
        member = await context.bot.get_chat_member(
            chat_id=f"@{CHANNEL_USERNAME}",
            user_id=user_id
        )
        return member.status in ['member', 'administrator', 'creator']
    except TelegramError as e:
        logger.error(f"Ошибка проверки подписки для {user_id}: {e}")
        return False


def calculate_result(answers: dict) -> str:
    """Подсчитывает результат теста по ответам."""
    counts = {"A": 0, "B": 0, "C": 0, "D": 0}
    for answer in answers.values():
        if answer in counts:
            counts[answer] += 1

    # Находим тип с максимальным количеством ответов
    result_type = max(counts, key=counts.get)
    return result_type


def resolve_video_source(video_source: str) -> str:
    """Возвращает путь к локальному видео, если он существует."""
    if not video_source:
        return ''

    if os.path.isabs(video_source) and os.path.isfile(video_source):
        return video_source

    local_path = os.path.join(BASE_DIR, video_source)
    if os.path.isfile(local_path):
        return local_path

    return video_source


def build_question_keyboard(question_index: int) -> InlineKeyboardMarkup:
    """Строит клавиатуру для вопроса теста — только кнопки A B C D в одну строку."""
    keyboard = [[
        InlineKeyboardButton(key, callback_data=f"answer_{question_index}_{key}")
        for key in QUESTIONS[question_index]["options"].keys()
    ]]
    return InlineKeyboardMarkup(keyboard)


def format_question_text(question_index: int) -> str:
    """Формирует текст вопроса с вариантами ответов."""
    q = QUESTIONS[question_index]
    text = (
        f"📝 <b>Тест: «Играешь роль или живёшь?»</b>\n\n"
        f"Вопрос {question_index + 1} из {len(QUESTIONS)}\n\n"
        f"<b>{q['text']}</b>\n\n"
    )
    for key, option in q["options"].items():
        text += f"<b>{key}</b> — {option}\n\n"
    text += "👇 Выбери свой вариант:"
    return text


async def send_video_or_text(
    update_or_chat_id,
    context: ContextTypes.DEFAULT_TYPE,
    video_file_id: str,
    caption: str = None,
    text_fallback: str = None,
    reply_markup=None,
    video_as_note: bool = False,
):
    """Отправляет видео или видео-ноту, если file_id задан, иначе текст."""
    # Определяем chat_id
    if isinstance(update_or_chat_id, int):
        chat_id = update_or_chat_id
    else:
        chat_id = update_or_chat_id.effective_chat.id

    if video_file_id:
        video_source = resolve_video_source(video_file_id)
        try:
            if video_as_note:
                await context.bot.send_video_note(
                    chat_id=chat_id,
                    video_note=video_source,
                    reply_markup=None,
                )
                # После отправки кружочка покажем текст и кнопку
                msg = caption or text_fallback or ""
                if msg:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=msg,
                        reply_markup=reply_markup,
                        parse_mode='HTML'
                    )
                return
            else:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=video_source,
                    caption=caption,
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
                return
        except TelegramError as e:
            logger.error(f"Ошибка отправки видео: {e}")

    # Если видео нет или ошибка — отправляем текст
    msg = caption or text_fallback or ""
    if msg:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=msg,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        except TelegramError as e:
            logger.error(f"Ошибка отправки текста: {e}")


# ============ ОБРАБОТЧИКИ КОМАНД ============
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start."""
    user = update.effective_user
    db.save_user(user.id, user.username, user.first_name, user.last_name)

    # Сбрасываем данные пользователя
    context.user_data.clear()
    context.user_data['answers'] = {}
    context.user_data['current_question'] = 0

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎭 Пройти тест", callback_data="start_quiz")]
    ])

    # Отправляем приветственное видео (или текст)
    await send_video_or_text(
        update,
        context,
        video_file_id=WELCOME_VIDEO,
        caption=MESSAGES['welcome'],
        text_fallback=MESSAGES['welcome'],
        reply_markup=keyboard,
        video_as_note=WELCOME_VIDEO_AS_NOTE,
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика для администратора."""
    user = update.effective_user
    if ADMIN_ID and user.id != ADMIN_ID:
        return

    stats = db.get_stats()
    by_type = stats['by_type']

    text = (
        "📊 <b>Статистика бота</b>\n\n"
        f"👥 Всего пользователей: {stats['total']}\n"
        f"✅ Прошли тест: {stats['finished']}\n\n"
        f"📋 По типам:\n"
        f"  Тип A (Ищу выход из роли): {by_type.get('A', 0)}\n"
        f"  Тип B (Вижу свет): {by_type.get('B', 0)}\n"
        f"  Тип C (Всё своё ношу): {by_type.get('C', 0)}\n"
        f"  Тип D (Глубокий, масштабный): {by_type.get('D', 0)}\n\n"
        f"🔥 Хотят на интенсив: {stats['intensive_yes']}\n"
        f"❌ Отказались от интенсива: {stats['intensive_no']}"
    )

    await update.message.reply_text(text, parse_mode='HTML')


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help."""
    text = (
        "🤖 <b>Бот Дарины</b>\n\n"
        "/start — Начать сначала\n"
        "/help — Помощь\n\n"
        "Пройди тест «Играешь роль или живёшь?» "
        "и узнай свой тип проявленности в видеоконтенте! 🎬"
    )
    await update.message.reply_text(text, parse_mode='HTML')


async def cmd_setvideo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Команда для администратора: установить видео.
    Использование: отправьте видео с подписью:
    /setvideo welcome
    /setvideo result_a
    /setvideo result_b
    /setvideo result_c
    /setvideo result_d
    /setvideo consultation
    """
    user = update.effective_user
    if ADMIN_ID and user.id != ADMIN_ID:
        return

    if not update.message.reply_to_message or not update.message.reply_to_message.video:
        await update.message.reply_text(
            "📹 Чтобы установить видео:\n"
            "1. Отправьте видео в чат\n"
            "2. Ответьте на него командой:\n\n"
            "<code>/setvideo welcome</code> — приветственное\n"
            "<code>/setvideo result_a</code> — для типа A\n"
            "<code>/setvideo result_b</code> — для типа B\n"
            "<code>/setvideo result_c</code> — для типа C\n"
            "<code>/setvideo result_d</code> — для типа D\n"
            "<code>/setvideo consultation</code> — для консультации",
            parse_mode='HTML'
        )
        return

    args = context.args
    if not args:
        await update.message.reply_text("❌ Укажите тип видео (welcome, result_a, result_b, result_c, result_d, consultation)")
        return

    video_type = args[0].lower()
    file_id = update.message.reply_to_message.video.file_id

    valid_types = ['welcome', 'result_a', 'result_b', 'result_c', 'result_d', 'consultation']
    if video_type not in valid_types:
        await update.message.reply_text(f"❌ Неизвестный тип. Доступные: {', '.join(valid_types)}")
        return

    # Сохраняем file_id в bot_data для использования
    context.bot_data[f'video_{video_type}'] = file_id

    await update.message.reply_text(
        f"✅ Видео <b>{video_type}</b> установлено!\n"
        f"file_id: <code>{file_id}</code>\n\n"
        "💡 Чтобы видео сохранилось после перезапуска, "
        "добавьте file_id в config.py или .env файл.",
        parse_mode='HTML'
    )


def get_video(context: ContextTypes.DEFAULT_TYPE, video_type: str, default: str = '') -> str:
    """Получает file_id видео: сначала из bot_data (динамически), потом из config."""
    return context.bot_data.get(f'video_{video_type}', default)


# ============ ОБРАБОТЧИКИ CALLBACK ============
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главный обработчик inline-кнопок."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "start_quiz":
        await start_quiz(update, context)

    elif data.startswith("answer_"):
        await handle_answer(update, context)

    elif data == "check_subscription":
        await handle_check_subscription(update, context)

    elif data == "intensive_yes":
        await handle_intensive_yes(update, context)

    elif data == "intensive_no":
        await handle_intensive_no(update, context)

    elif data.startswith("objection_"):
        await handle_objection(update, context)

    elif data == "intensive_reconsider_yes":
        await handle_intensive_yes(update, context)

    elif data == "intensive_reconsider_no":
        await handle_final_no(update, context)


# ============ КВИЗ ============
async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начинает тест: отправляет первый вопрос."""
    context.user_data['answers'] = {}
    context.user_data['current_question'] = 0

    q_index = 0
    keyboard = build_question_keyboard(q_index)

    await update.callback_query.edit_message_text(
        text=format_question_text(q_index),
        reply_markup=keyboard,
        parse_mode='HTML'
    )


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ответ на вопрос теста."""
    query = update.callback_query
    data = query.data  # answer_0_A

    parts = data.split("_")
    q_index = int(parts[1])
    answer = parts[2]

    # Сохраняем ответ
    if 'answers' not in context.user_data:
        context.user_data['answers'] = {}
    context.user_data['answers'][q_index] = answer

    next_index = q_index + 1

    if next_index < len(QUESTIONS):
        # Следующий вопрос
        context.user_data['current_question'] = next_index
        keyboard = build_question_keyboard(next_index)

        await query.edit_message_text(
            text=format_question_text(next_index),
            reply_markup=keyboard,
            parse_mode='HTML'
        )
    else:
        # Тест завершён — требуем подписку
        result_type = calculate_result(context.user_data['answers'])
        context.user_data['result_type'] = result_type

        # Сохраняем в БД
        answers_str = ",".join(f"{k}:{v}" for k, v in sorted(context.user_data['answers'].items()))
        db.save_result(update.effective_user.id, result_type, answers_str)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Подписаться на канал", url=f"https://t.me/{CHANNEL_USERNAME}")],
            [InlineKeyboardButton("✅ Я подписался(ась)", callback_data="check_subscription")]
        ])

        await query.edit_message_text(
            text=(
                "🎉 <b>Тест пройден!</b>\n\n"
                f"{MESSAGES['subscribe_required']}"
            ),
            reply_markup=keyboard,
            parse_mode='HTML'
        )


# ============ ПРОВЕРКА ПОДПИСКИ ============
async def handle_check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверяет подписку и выдаёт результат."""
    query = update.callback_query
    user_id = update.effective_user.id

    is_subscribed = await check_subscription(user_id, context)

    if not is_subscribed:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Подписаться на канал", url=f"https://t.me/{CHANNEL_USERNAME}")],
            [InlineKeyboardButton("✅ Я подписался(ась)", callback_data="check_subscription")]
        ])

        await query.edit_message_text(
            text=MESSAGES['not_subscribed'],
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        return

    # Подписан — выдаём результат
    result_type = context.user_data.get('result_type', 'A')
    result = RESULTS[result_type]

    # Редактируем старое сообщение и предлагаем получить подкаст
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎧 Получить подкаст", url="https://t.me/Dana_lavanda")]
    ])

    await query.edit_message_text(
        text=f"✅ Подписка подтверждена! Вот твой результат 👇",
        reply_markup=keyboard,
        parse_mode='HTML'
    )

    # Отправляем результат теста отдельным сообщением
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"<b>{result['title']}</b>\n\n{result['text']}",
        parse_mode='HTML'
    )


async def send_intensive_offer(chat_id: int, context: ContextTypes.DEFAULT_TYPE, result_type: str):
    """Отправляет предложение об интенсиве (типы A, B, C)."""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Да, хочу на интенсив!", callback_data="intensive_yes")],
        [InlineKeyboardButton("🚫 Нет, пока не хочу", callback_data="intensive_no")]
    ])

    await context.bot.send_message(
        chat_id=chat_id,
        text=MESSAGES['intensive_offer'],
        reply_markup=keyboard,
        parse_mode='HTML'
    )


async def send_consultation_offer(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет предложение о консультации (тип D)."""
    # Видео-приглашение на консультацию
    consultation_video = get_video(context, 'consultation', VIDEO_CONSULTATION)
    if consultation_video:
        try:
            await context.bot.send_video(
                chat_id=chat_id,
                video=consultation_video,
                caption="🌟 Персональное приглашение от Дарины 👆"
            )
        except TelegramError as e:
            logger.error(f"Ошибка отправки видео консультации: {e}")

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 Записаться на консультацию", url="https://t.me/Dana_lavanda")]
    ])

    await context.bot.send_message(
        chat_id=chat_id,
        text=MESSAGES['consultation_offer'],
        reply_markup=keyboard,
        parse_mode='HTML'
    )


# ============ ВОРОНКА: ИНТЕНСИВ ============
async def handle_intensive_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пользователь хочет на интенсив."""
    query = update.callback_query
    user_id = update.effective_user.id
    result_type = context.user_data.get('result_type', 'A')
    result_title = RESULTS[result_type]['title']

    db.save_intensive_response(user_id, 'yes')

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 Написать администратору", url="https://t.me/Dana_lavanda")]
    ])

    msg = MESSAGES['intensive_yes'].format(result_type=result_title)

    await query.edit_message_text(
        text=msg,
        reply_markup=keyboard,
        parse_mode='HTML'
    )

    # Уведомляем админа
    if ADMIN_ID:
        user = update.effective_user
        admin_msg = (
            f"🔔 <b>Новая заявка на интенсив!</b>\n\n"
            f"👤 {user.first_name or ''} {user.last_name or ''}\n"
            f"📱 @{user.username or 'нет username'}\n"
            f"🆔 {user.id}\n"
            f"📋 Результат: {result_title}"
        )
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=admin_msg,
                parse_mode='HTML'
            )
        except TelegramError as e:
            logger.error(f"Не удалось уведомить админа: {e}")


async def handle_intensive_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пользователь не хочет на интенсив — начинаем обработку возражений."""
    query = update.callback_query
    user_id = update.effective_user.id

    db.save_intensive_response(user_id, 'no')

    # Первый вопрос из воронки возражений
    objection = OBJECTION_QUESTIONS[0]

    keyboard = []
    for key, text in objection["options"].items():
        keyboard.append([
            InlineKeyboardButton(text, callback_data=f"objection_{key}")
        ])

    await query.edit_message_text(
        text=objection["text"],
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def handle_objection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ответа на возражение."""
    query = update.callback_query
    user_id = update.effective_user.id
    reason = query.data.replace("objection_", "")

    db.save_objection(user_id, reason)

    # Получаем ответ для этого возражения
    responses = OBJECTION_QUESTIONS[1]
    response_data = responses.get(reason)

    if response_data:
        # Предлагаем ещё раз
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔥 Да, попробую!", callback_data="intensive_reconsider_yes")],
            [InlineKeyboardButton("Нет, спасибо 🙏", callback_data="intensive_reconsider_no")]
        ])

        await query.edit_message_text(
            text=response_data["text"],
            reply_markup=keyboard,
            parse_mode='HTML'
        )
    else:
        await handle_final_no(update, context)


async def handle_final_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Финальное сообщение для тех, кто окончательно отказался."""
    query = update.callback_query
    final_message = OBJECTION_QUESTIONS[2]

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Написать Дарине", url="https://t.me/Dana_lavanda")],
        [InlineKeyboardButton("🔄 Пройти тест заново", callback_data="start_quiz")]
    ])

    await query.edit_message_text(
        text=final_message["text"],
        reply_markup=keyboard,
        parse_mode='HTML'
    )


# ============ ОБРАБОТКА ВИДЕО (для получения file_id) ============
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Когда пользователь отправляет видео — показывает file_id (для админа)."""
    user = update.effective_user
    if ADMIN_ID and user.id != ADMIN_ID:
        return

    video = update.message.video or update.message.video_note
    if video:
        await update.message.reply_text(
            f"📹 <b>Video file_id:</b>\n<code>{video.file_id}</code>\n\n"
            "Используйте это значение в config.py или .env\n"
            "Или ответьте на это видео командой /setvideo <тип>",
            parse_mode='HTML'
        )


# ============ ОБРАБОТКА ТЕКСТА ============
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстовых сообщений."""
    # Если пользователь пишет после возражения "другая причина"
    user_id = update.effective_user.id

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Пройти тест", callback_data="start_quiz")],
        [InlineKeyboardButton("💬 Написать Дарине", url="https://t.me/Dana_lavanda")]
    ])

    await update.message.reply_text(
        "Спасибо за сообщение! 🤗\n\n"
        "Если хочешь пройти тест — нажми кнопку ниже.\n"
        "Или напиши напрямую Дарине: @Dana_lavanda",
        reply_markup=keyboard,
        parse_mode='HTML'
    )

    # Пересылаем админу
    if ADMIN_ID:
        try:
            user = update.effective_user
            admin_text = (
                f"💬 <b>Сообщение от пользователя:</b>\n\n"
                f"👤 {user.first_name or ''} @{user.username or 'нет'}\n"
                f"🆔 {user.id}\n\n"
                f"📝 {update.message.text}"
            )
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=admin_text,
                parse_mode='HTML'
            )
        except TelegramError:
            pass


# ============ ОШИБКИ ============
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок."""
    logger.error(f"Ошибка: {context.error}", exc_info=context.error)


# ============ ЗАПУСК ============
def main():
    """Запуск бота."""
    request = HTTPXRequest(
        connect_timeout=15.0,
        read_timeout=25.0,
        write_timeout=25.0,
        pool_timeout=5.0,
        http_version='1.1'
    )
    get_updates_request = HTTPXRequest(
        connect_timeout=15.0,
        read_timeout=90.0,
        write_timeout=25.0,
        pool_timeout=5.0,
        http_version='1.1'
    )
    application = Application.builder()
    application = application.request(request)
    application = application.get_updates_request(get_updates_request)
    application = application.token(BOT_TOKEN).build()

    # Команды
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("setvideo", cmd_setvideo))

    # Inline-кнопки
    application.add_handler(CallbackQueryHandler(callback_handler))

    # Видео от админа
    application.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, handle_video))

    # Текстовые сообщения
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Ошибки
    application.add_error_handler(error_handler)

    logger.info("🚀 Бот Дарины запущен!")
    application.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
