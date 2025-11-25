import os
import uuid
import logging
import re
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackContext, filters
from pydub import AudioSegment
import speech_recognition as sr
import httpx

# Google Calendar imports
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN')
TODOIST_API_TOKEN = os.getenv("TODOIST_API_TOKEN")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")

# Проверка наличия токенов
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан! Установите переменную окружения.")
if not TODOIST_API_TOKEN:
    raise ValueError("TODOIST_API_TOKEN не задан! Установите переменную окружения.")

# Хранение режима для каждого пользователя
user_modes = {}

# Режимы работы
MODE_TASK = "task"
MODE_MEETING = "meeting"

# Клавиатуры меню
def get_main_keyboard():
    """Создание главной клавиатуры выбора"""
    keyboard = [
        [KeyboardButton("📝 Задача"), KeyboardButton("📅 Встреча")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_mode_keyboard():
    """Клавиатура после выбора режима (с кнопкой назад)"""
    keyboard = [
        [KeyboardButton("◀️ Назад")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_google_calendar_service():
    """Получение сервиса Google Calendar"""
    credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not credentials_json:
        return None
    
    import json
    credentials_info = json.loads(credentials_json)
    credentials = service_account.Credentials.from_service_account_info(
        credentials_info,
        scopes=['https://www.googleapis.com/auth/calendar']
    )
    return build('calendar', 'v3', credentials=credentials)


def parse_meeting_time(text: str):
    """
    Парсинг времени встречи из текста.
    Примеры: "встреча завтра в 15:00", "созвон в 10:30", "митинг в 14:00"
    Возвращает (название, start_time, end_time) или (text, None, None)
    """
    # Паттерны для времени
    time_pattern = r'(\d{1,2})[:\.](\d{2})'
    time_match = re.search(time_pattern, text)
    
    if not time_match:
        # Если время не указано, создаём событие на следующий час
        now = datetime.now()
        start_time = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        end_time = start_time + timedelta(hours=1)
        return text, start_time, end_time
    
    hour = int(time_match.group(1))
    minute = int(time_match.group(2))
    
    # Определяем дату
    today = datetime.now()
    
    if "завтра" in text.lower():
        meeting_date = today + timedelta(days=1)
    elif "послезавтра" in text.lower():
        meeting_date = today + timedelta(days=2)
    else:
        meeting_date = today
        # Если время уже прошло, ставим на завтра
        if hour < today.hour or (hour == today.hour and minute <= today.minute):
            meeting_date = today + timedelta(days=1)
    
    start_time = meeting_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
    end_time = start_time + timedelta(hours=1)
    
    # Убираем время из названия для чистоты
    title = re.sub(time_pattern, '', text)
    title = re.sub(r'\s*(в|на|завтра|послезавтра)\s*', ' ', title, flags=re.IGNORECASE)
    title = ' '.join(title.split()).strip()
    
    if not title:
        title = "Встреча"
    
    return title, start_time, end_time


async def start(update: Update, context: CallbackContext) -> None:
    """Приветственное сообщение с меню"""
    user_id = update.effective_user.id
    user_modes[user_id] = MODE_TASK  # По умолчанию режим задач
    
    await update.message.reply_text(
        "👋 Привет! Я помогу создать задачу или встречу.\n\n"
        "📝 *Задача* — отправлю в Todoist\n"
        "📅 *Встреча* — добавлю в Google Calendar\n\n"
        "Выбери режим и отправь голосовое сообщение!",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )


async def handle_mode_selection(update: Update, context: CallbackContext) -> None:
    """Обработка выбора режима"""
    user_id = update.effective_user.id
    text = update.message.text
    
    if "📝 Задача" in text:
        user_modes[user_id] = MODE_TASK
        await update.message.reply_text(
            "✅ Выбрано: *Задача*\n\n"
            "🎤 Отправь голосовое сообщение, и я создам задачу в Todoist.\n\n"
            "_Нажми «◀️ Назад» чтобы выбрать другое действие._",
            reply_markup=get_mode_keyboard(),
            parse_mode="Markdown"
        )
    elif "📅 Встреча" in text:
        user_modes[user_id] = MODE_MEETING
        await update.message.reply_text(
            "✅ Выбрано: *Встреча*\n\n"
            "🎤 Отправь голосовое сообщение с описанием встречи.\n"
            "Можешь указать время, например:\n"
            "• _«Созвон с командой в 15:00»_\n"
            "• _«Встреча завтра в 10:30»_\n\n"
            "_Нажми «◀️ Назад» чтобы выбрать другое действие._",
            reply_markup=get_mode_keyboard(),
            parse_mode="Markdown"
        )


async def handle_back(update: Update, context: CallbackContext) -> None:
    """Обработка кнопки Назад"""
    user_id = update.effective_user.id
    user_modes[user_id] = None  # Сбрасываем режим
    
    await update.message.reply_text(
        "◀️ Вернулись в меню.\n\nВыбери что хочешь сделать:",
        reply_markup=get_main_keyboard()
    )


async def get_chat_id(update: Update, context: CallbackContext) -> None:
    """Получение chat_id для отладки"""
    chat_id = update.message.chat_id
    logger.info(f"Chat ID: {chat_id}")
    await update.message.reply_text(f"Chat ID: {chat_id}")


async def recognize_voice(file_path: str, wav_path: str) -> str:
    """Распознавание голосового сообщения"""
    audio = AudioSegment.from_file(file_path)
    audio.export(wav_path, format="wav")
    
    recognizer = sr.Recognizer()
    with sr.AudioFile(wav_path) as source:
        audio_data = recognizer.record(source)
        return recognizer.recognize_google(audio_data, language="ru-RU")


async def create_todoist_task(text: str) -> tuple[bool, str]:
    """Создание задачи в Todoist"""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.todoist.com/rest/v2/tasks",
            headers={"Authorization": f"Bearer {TODOIST_API_TOKEN}"},
            json={"content": text},
        )
    
    if response.status_code == 200:
        return True, text
    else:
        return False, response.text


async def create_calendar_event(text: str) -> tuple[bool, str]:
    """Создание события в Google Calendar"""
    service = get_google_calendar_service()
    
    if not service:
        return False, "Google Calendar не настроен. Добавьте GOOGLE_CREDENTIALS_JSON."
    
    title, start_time, end_time = parse_meeting_time(text)
    
    event = {
        'summary': title,
        'start': {
            'dateTime': start_time.isoformat(),
            'timeZone': 'Europe/Moscow',
        },
        'end': {
            'dateTime': end_time.isoformat(),
            'timeZone': 'Europe/Moscow',
        },
    }
    
    try:
        created_event = service.events().insert(
            calendarId=GOOGLE_CALENDAR_ID,
            body=event
        ).execute()
        
        event_time = start_time.strftime("%d.%m.%Y в %H:%M")
        return True, f"{title}\n🕐 {event_time}"
    except Exception as e:
        logger.error(f"Ошибка Google Calendar: {e}")
        return False, str(e)


async def handle_voice(update: Update, context: CallbackContext) -> None:
    """Обработка голосовых сообщений"""
    user_id = update.effective_user.id
    mode = user_modes.get(user_id, MODE_TASK)
    
    unique_id = uuid.uuid4().hex
    file_path = f"voice_{unique_id}.ogg"
    wav_path = f"voice_{unique_id}.wav"

    try:
        # Уведомляем пользователя
        if mode == MODE_TASK:
            processing_msg = await update.message.reply_text("🎙 Создаю задачу...")
        else:
            processing_msg = await update.message.reply_text("🎙 Создаю встречу...")

        # Скачиваем аудио
        voice_file = await update.message.voice.get_file()
        await voice_file.download_to_drive(file_path)

        # Распознаём текст
        text = await recognize_voice(file_path, wav_path)
        logger.info(f"Распознан текст: {text}")

        # Выполняем действие в зависимости от режима
        if mode == MODE_TASK:
            success, result = await create_todoist_task(text)
            if success:
                await processing_msg.edit_text(f"✅ Задача добавлена в Todoist:\n\n📝 {result}")
            else:
                await processing_msg.edit_text(f"❌ Ошибка добавления задачи: {result}")
        else:
            success, result = await create_calendar_event(text)
            if success:
                await processing_msg.edit_text(f"✅ Встреча добавлена в календарь:\n\n📅 {result}")
            else:
                await processing_msg.edit_text(f"❌ Ошибка создания встречи: {result}")

        # Показываем меню выбора после действия
        await update.message.reply_text(
            "Что дальше? Выбери действие:",
            reply_markup=get_main_keyboard()
        )

    except sr.UnknownValueError:
        await update.message.reply_text(
            "🤷 Не удалось распознать текст. Попробуйте ещё раз.",
            reply_markup=get_main_keyboard()
        )
    except sr.RequestError as e:
        await update.message.reply_text(
            f"⚠️ Ошибка сервиса распознавания: {e}",
            reply_markup=get_main_keyboard()
        )
        logger.error(f"Ошибка Speech Recognition: {e}")
    except Exception as e:
        await update.message.reply_text(
            f"❌ Произошла ошибка: {e}",
            reply_markup=get_main_keyboard()
        )
        logger.error(f"Неожиданная ошибка: {e}")
    finally:
        for path in [file_path, wav_path]:
            if os.path.exists(path):
                os.remove(path)


def main() -> None:
    """Запуск бота"""
    app = Application.builder().token(BOT_TOKEN).connect_timeout(30).read_timeout(30).build()

    # Обработчики
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", get_chat_id))
    
    # Обработка кнопок меню
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(r'^(📝 Задача|📅 Встреча)$'),
        handle_mode_selection
    ))
    
    # Обработка кнопки Назад
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(r'^◀️ Назад$'),
        handle_back
    ))
    
    # Обработка голосовых сообщений
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    logger.info("Бот запущен!")
    app.run_polling()


if __name__ == '__main__':
    main()
