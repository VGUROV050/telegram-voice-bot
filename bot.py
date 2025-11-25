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
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
NOTION_API_KEY = os.getenv("NOTION_API_KEY")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")

# Проверка наличия токенов
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан! Установите переменную окружения.")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY не задан! Установите переменную окружения.")
if not NOTION_API_KEY:
    raise ValueError("NOTION_API_KEY не задан! Установите переменную окружения.")
if not NOTION_DATABASE_ID:
    raise ValueError("NOTION_DATABASE_ID не задан! Установите переменную окружения.")

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


async def parse_meeting_with_ai(text: str) -> tuple[str, datetime, datetime]:
    """
    Умный парсинг встречи через GPT.
    Понимает естественный язык: "через пару часов", "на следующей неделе", "перед обедом" и т.д.
    """
    import json
    
    now = datetime.now()
    current_datetime = now.strftime("%Y-%m-%d %H:%M")
    current_weekday = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"][now.weekday()]
    
    # Вычисляем даты для примеров
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    day_after_tomorrow = (now + timedelta(days=2)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")
    
    system_prompt = f"""Ты помощник для извлечения информации о встречах из текста.

ВАЖНО! Сегодня: {today} ({current_weekday}), текущее время: {now.strftime("%H:%M")}
- завтра = {tomorrow}
- послезавтра = {day_after_tomorrow}

Извлеки из текста:
1. Название встречи — ТОЛЬКО суть встречи (с кем/что)
2. Дату в формате YYYY-MM-DD
3. Время в формате HH:MM
4. Длительность (по умолчанию 60 минут)

ПРАВИЛА ДЛЯ НАЗВАНИЯ:
- УДАЛИ слова: "поставь", "запиши", "создай", "добавь", "встречу", "встреча", "созвон"
- Оставь только суть: "с Антоном", "с командой", "у врача" и т.д.
- Добавь "Встреча" в начало если нужно для читаемости
- Примеры:
  - "поставь встречу с Антоном" → "Встреча с Антоном"
  - "созвон с командой" → "Созвон с командой" 
  - "записаться к врачу" → "Врач"

ПРАВИЛА ДЛЯ ДАТЫ:
- "завтра" = {tomorrow} (НЕ ДРУГАЯ ДАТА!)
- "послезавтра" = {day_after_tomorrow}
- "сегодня" = {today}
- "через N дней" = {today} + N дней
- "в понедельник/вторник/..." = ближайший такой день

ПРАВИЛА ДЛЯ ВРЕМЕНИ:
- "утром" = 09:00
- "днём" = 13:00  
- "вечером" = 18:00
- "после обеда" = 14:00
- Если не указано = 10:00

Верни ТОЛЬКО JSON (без ```):
{{"title": "название", "date": "YYYY-MM-DD", "time": "HH:MM", "duration": 60}}"""

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ],
            temperature=0,
            max_tokens=150
        )
        
        result_text = response.choices[0].message.content.strip()
        logger.info(f"GPT ответ: {result_text}")
        
        # Парсим JSON
        # Убираем возможные markdown-обёртки
        if result_text.startswith("```"):
            result_text = result_text.split("```")[1]
            if result_text.startswith("json"):
                result_text = result_text[4:]
        result_text = result_text.strip()
        
        data = json.loads(result_text)
        
        title = data.get("title", "Встреча")
        date_str = data.get("date", now.strftime("%Y-%m-%d"))
        time_str = data.get("time", "10:00")
        duration = data.get("duration", 60)
        
        # Собираем datetime
        start_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        end_time = start_time + timedelta(minutes=duration)
        
        logger.info(f"AI парсинг: '{text}' -> title='{title}', start={start_time}, end={end_time}")
        
        return title, start_time, end_time
        
    except Exception as e:
        logger.error(f"Ошибка AI парсинга: {e}, использую fallback")
        # Fallback на простой парсинг
        return parse_meeting_time_simple(text)


def parse_meeting_time_simple(text: str) -> tuple[str, datetime, datetime]:
    """Простой fallback-парсер на regex"""
    now = datetime.now()
    
    # Ищем время
    time_match = re.search(r'(\d{1,2})[:\.](\d{2})', text)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
    else:
        hour, minute = 10, 0
    
    # Ищем дату
    days_offset = 0
    text_lower = text.lower()
    if 'завтра' in text_lower:
        days_offset = 1
    elif 'послезавтра' in text_lower:
        days_offset = 2
    
    start_time = (now + timedelta(days=days_offset)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    end_time = start_time + timedelta(hours=1)
    
    # Название - убираем время и даты
    title = re.sub(r'\d{1,2}[:\.]?\d{0,2}', '', text)
    title = re.sub(r'(завтра|послезавтра|сегодня|в|на|утром|вечером|днём)', '', title, flags=re.IGNORECASE)
    title = ' '.join(title.split()).strip() or "Встреча"
    
    return title, start_time, end_time


async def start(update: Update, context: CallbackContext) -> None:
    """Приветственное сообщение с меню"""
    user_id = update.effective_user.id
    user_modes[user_id] = MODE_TASK  # По умолчанию режим задач
    
    await update.message.reply_text(
        "👋 Привет! Я помогу создать задачу или встречу.\n\n"
        "📝 *Задача* — отправлю в Notion\n"
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
            "🎤 Отправь голосовое сообщение, и я создам задачу в Notion.\n\n"
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


async def create_notion_task(text: str) -> tuple[bool, str]:
    """Создание задачи в Notion"""
    
    # Формируем данные для Notion API
    notion_data = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {
            "Title": {
                "title": [
                    {
                        "text": {
                            "content": text
                        }
                    }
                ]
            },
            "Status Update": {
                "select": {
                    "name": "Not started"
                }
            },
            "Assigned to": {
                "select": {
                    "name": "Vlad"
                }
            }
        }
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.notion.com/v1/pages",
            headers={
                "Authorization": f"Bearer {NOTION_API_KEY}",
                "Content-Type": "application/json",
                "Notion-Version": "2022-06-28"
            },
            json=notion_data,
        )
    
    if response.status_code == 200:
        logger.info(f"Задача создана в Notion: {text}")
        return True, text
    else:
        logger.error(f"Ошибка Notion API: {response.status_code} - {response.text}")
        return False, response.text


async def create_calendar_event(text: str) -> tuple[bool, str]:
    """Создание события в Google Calendar"""
    logger.info(f"Создаю событие в календаре: {text}")
    
    service = get_google_calendar_service()
    
    if not service:
        logger.error("Google Calendar сервис не создан!")
        return False, "Google Calendar не настроен. Добавьте GOOGLE_CREDENTIALS_JSON."
    
    title, start_time, end_time = await parse_meeting_with_ai(text)
    
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
            success, result = await create_notion_task(text)
            if success:
                await processing_msg.edit_text(f"✅ Задача добавлена в Notion:\n\n📝 {result}")
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
