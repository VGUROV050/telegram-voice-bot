import os
import uuid
import logging
import re
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackContext, filters
from pydub import AudioSegment
import httpx

# Google Calendar imports
from google.oauth2 import service_account
from googleapiclient.discovery import build

# OpenAI Whisper для распознавания речи
from openai import OpenAI

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
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Проверка наличия токенов
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан! Установите переменную окружения.")
if not TODOIST_API_TOKEN:
    raise ValueError("TODOIST_API_TOKEN не задан! Установите переменную окружения.")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY не задан! Установите переменную окружения.")

# Инициализация OpenAI клиента
openai_client = OpenAI(api_key=OPENAI_API_KEY)

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
        logger.error("GOOGLE_CREDENTIALS_JSON не задан!")
        return None
    
    try:
        import json
        credentials_info = json.loads(credentials_json)
        logger.info(f"Google credentials загружены для: {credentials_info.get('client_email', 'unknown')}")
        
        credentials = service_account.Credentials.from_service_account_info(
            credentials_info,
            scopes=['https://www.googleapis.com/auth/calendar']
        )
        # cache_discovery=False убирает предупреждение о file_cache
        return build('calendar', 'v3', credentials=credentials, cache_discovery=False)
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка парсинга GOOGLE_CREDENTIALS_JSON: {e}")
        return None
    except Exception as e:
        logger.error(f"Ошибка создания Google Calendar сервиса: {e}")
        return None


def parse_meeting_time(text: str):
    """
    Парсинг времени встречи из текста.
    Понимает:
    - Точное время: 15:00, 10.30
    - Время суток: утро/утром (9:00), день/днём (13:00), вечер/вечером (18:00)
    - Относительные даты: сегодня, завтра, послезавтра, после завтра
    - Дни недели: понедельник, вторник, среда и т.д.
    - Через N часов/минут
    """
    text_lower = text.lower()
    today = datetime.now()
    
    # Нормализация текста: "после завтра" -> "послезавтра"
    text_lower = re.sub(r'после\s+завтра', 'послезавтра', text_lower)
    
    # === ОПРЕДЕЛЯЕМ ВРЕМЯ ===
    hour = None
    minute = 0
    
    # 1. Точное время: 15:00, 10.30, 9 00
    time_pattern = r'(\d{1,2})[:\.\s](\d{2})'
    time_match = re.search(time_pattern, text_lower)
    
    # 2. Просто час: "в 9", "в 15"
    hour_only_pattern = r'\bв\s*(\d{1,2})\b(?!\s*[:\.]?\s*\d)'
    hour_only_match = re.search(hour_only_pattern, text_lower)
    
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
    elif hour_only_match:
        hour = int(hour_only_match.group(1))
        minute = 0
    
    # 3. Время суток
    time_of_day_map = {
        'утр': 9,      # утро, утром
        'днём': 13, 'днем': 13, ' день': 13,
        'вечер': 18,   # вечер, вечером
        'ночь': 21, 'ночью': 21,
    }
    
    if hour is None:
        for keyword, default_hour in time_of_day_map.items():
            if keyword in text_lower:
                hour = default_hour
                break
    
    # Если время всё ещё не определено - ставим через час
    if hour is None:
        start_time = today.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:
        start_time = today.replace(hour=hour, minute=minute, second=0, microsecond=0)
    
    # === ОПРЕДЕЛЯЕМ ДАТУ ===
    days_offset = 0
    
    # Дни недели
    weekdays = {
        'понедельник': 0, 'пн': 0,
        'вторник': 1, 'вт': 1,
        'сред': 2, 'ср': 2,          # среда, среду
        'четверг': 3, 'чт': 3,
        'пятниц': 4, 'пт': 4,        # пятница, пятницу
        'суббот': 5, 'сб': 5,        # суббота, субботу
        'воскресень': 6, 'вс': 6,    # воскресенье
    }
    
    found_weekday = None
    for day_name, day_num in weekdays.items():
        if day_name in text_lower:
            found_weekday = day_num
            break
    
    if found_weekday is not None:
        current_weekday = today.weekday()
        days_offset = (found_weekday - current_weekday) % 7
        if days_offset == 0:  # Если тот же день недели
            # Если время уже прошло - на следующую неделю
            if hour is not None and (hour < today.hour or (hour == today.hour and minute <= today.minute)):
                days_offset = 7
    elif 'послезавтра' in text_lower:
        days_offset = 2
    elif 'завтра' in text_lower:
        days_offset = 1
    elif 'сегодня' in text_lower:
        days_offset = 0
    else:
        # Если день не указан и время уже прошло - ставим на завтра
        if hour is not None and (hour < today.hour or (hour == today.hour and minute <= today.minute)):
            days_offset = 1
    
    # Применяем смещение даты
    meeting_date = today + timedelta(days=days_offset)
    if hour is not None:
        start_time = meeting_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
    else:
        start_time = meeting_date.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    
    end_time = start_time + timedelta(hours=1)
    
    # === ОЧИЩАЕМ НАЗВАНИЕ ===
    title = text
    # Убираем всё что связано с датой/временем
    patterns_to_remove = [
        r'\d{1,2}[:\.\s]\d{2}',  # время
        r'\bв\s*\d{1,2}\b',      # "в 9"
        r'\b(сегодня|завтра|послезавтра)\b',
        r'\b(понедельник|вторник|сред\w*|четверг|пятниц\w*|суббот\w*|воскресень\w*)\b',
        r'\b(пн|вт|ср|чт|пт|сб|вс)\b',
        r'\b(утр\w*|днём|днем|день|вечер\w*|ночь\w*)\b',
        r'\b(в|на|к)\b',
    ]
    
    for pattern in patterns_to_remove:
        title = re.sub(pattern, ' ', title, flags=re.IGNORECASE)
    
    title = ' '.join(title.split()).strip()
    
    if not title:
        title = "Встреча"
    
    logger.info(f"Парсинг: '{text}' -> title='{title}', date={start_time.strftime('%d.%m.%Y %H:%M')}")
    
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
        logger.info(f"Пользователь {user_id} выбрал режим: ЗАДАЧА")
        await update.message.reply_text(
            "✅ Выбрано: *Задача*\n\n"
            "🎤 Отправь голосовое сообщение, и я создам задачу в Todoist.\n\n"
            "_Нажми «◀️ Назад» чтобы выбрать другое действие._",
            reply_markup=get_mode_keyboard(),
            parse_mode="Markdown"
        )
    elif "📅 Встреча" in text:
        user_modes[user_id] = MODE_MEETING
        logger.info(f"Пользователь {user_id} выбрал режим: ВСТРЕЧА")
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


async def recognize_voice(file_path: str) -> str:
    """Распознавание голосового сообщения через OpenAI Whisper"""
    # Конвертируем в mp3 для лучшей совместимости с Whisper
    audio = AudioSegment.from_file(file_path)
    mp3_path = file_path.replace('.ogg', '.mp3')
    audio.export(mp3_path, format="mp3")
    
    try:
        with open(mp3_path, "rb") as audio_file:
            transcript = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="ru",  # Указываем русский язык для лучшего качества
                response_format="text"
            )
        logger.info(f"Whisper распознал: {transcript}")
        return transcript.strip()
    finally:
        # Удаляем временный mp3 файл
        if os.path.exists(mp3_path):
            os.remove(mp3_path)


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
    logger.info(f"Создаю событие в календаре: {text}")
    
    service = get_google_calendar_service()
    
    if not service:
        logger.error("Google Calendar сервис не создан!")
        return False, "Google Calendar не настроен. Добавьте GOOGLE_CREDENTIALS_JSON."
    
    title, start_time, end_time = parse_meeting_time(text)
    logger.info(f"Парсинг встречи: title='{title}', start={start_time}, end={end_time}")
    
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
    mode = user_modes.get(user_id)
    
    # Логируем текущий режим
    logger.info(f"Пользователь {user_id} отправил голосовое. Текущий режим: {mode}")
    
    # Если режим не выбран, просим выбрать
    if mode is None:
        await update.message.reply_text(
            "⚠️ Сначала выбери что хочешь сделать:",
            reply_markup=get_main_keyboard()
        )
        return
    
    unique_id = uuid.uuid4().hex
    file_path = f"voice_{unique_id}.ogg"

    try:
        # Уведомляем пользователя
        if mode == MODE_TASK:
            processing_msg = await update.message.reply_text("🎙 Создаю задачу...")
            logger.info(f"Обрабатываю как ЗАДАЧУ для пользователя {user_id}")
        else:
            processing_msg = await update.message.reply_text("🎙 Создаю встречу...")
            logger.info(f"Обрабатываю как ВСТРЕЧУ для пользователя {user_id}")

        # Скачиваем аудио
        voice_file = await update.message.voice.get_file()
        await voice_file.download_to_drive(file_path)

        # Распознаём текст через Whisper
        text = await recognize_voice(file_path)
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

    except Exception as e:
        await update.message.reply_text(
            f"❌ Произошла ошибка: {e}",
            reply_markup=get_main_keyboard()
        )
        logger.error(f"Ошибка обработки голосового: {e}")
    finally:
        # Удаляем временный файл
        if os.path.exists(file_path):
            os.remove(file_path)


async def error_handler(update: Update, context: CallbackContext) -> None:
    """Глобальный обработчик ошибок"""
    logger.error(f"Произошла ошибка: {context.error}")
    import traceback
    logger.error(f"Traceback: {''.join(traceback.format_exception(type(context.error), context.error, context.error.__traceback__))}")
    
    if update and update.effective_message:
        await update.effective_message.reply_text(
            f"❌ Произошла ошибка: {context.error}",
            reply_markup=get_main_keyboard()
        )


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
    
    # Глобальный обработчик ошибок
    app.add_error_handler(error_handler)

    logger.info("Бот запущен!")
    app.run_polling()


if __name__ == '__main__':
    main()
