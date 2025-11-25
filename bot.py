import os
import uuid
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackContext, filters
from pydub import AudioSegment
import speech_recognition as sr
import httpx  # Асинхронные HTTP-запросы

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN')
TODOIST_API_TOKEN = os.getenv("TODOIST_API_TOKEN")

# Проверка наличия токенов
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан! Установите переменную окружения.")
if not TODOIST_API_TOKEN:
    raise ValueError("TODOIST_API_TOKEN не задан! Установите переменную окружения.")


async def start(update: Update, context: CallbackContext) -> None:
    """Приветственное сообщение"""
    await update.message.reply_text(
        "👋 Привет! Отправь мне голосовое сообщение, и я создам задачу в Todoist."
    )


async def get_chat_id(update: Update, context: CallbackContext) -> None:
    """Получение chat_id для отладки"""
    chat_id = update.message.chat_id
    logger.info(f"Chat ID: {chat_id}")
    await update.message.reply_text(f"Chat ID: {chat_id}")


async def handle_voice(update: Update, context: CallbackContext) -> None:
    """Обработка голосовых сообщений"""
    # Уникальные имена файлов для избежания конфликтов
    unique_id = uuid.uuid4().hex
    file_path = f"voice_{unique_id}.ogg"
    wav_path = f"voice_{unique_id}.wav"

    try:
        # Уведомляем пользователя о начале обработки
        processing_msg = await update.message.reply_text("🎙 Обрабатываю голосовое сообщение...")

        # Скачиваем аудио
        voice_file = await update.message.voice.get_file()
        await voice_file.download_to_drive(file_path)

        # Конвертируем в WAV
        audio = AudioSegment.from_file(file_path)
        audio.export(wav_path, format="wav")

        # Распознаём текст
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)
            text = recognizer.recognize_google(audio_data, language="ru-RU")

        # Отправляем задачу в Todoist (асинхронно)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.todoist.com/rest/v2/tasks",
                headers={"Authorization": f"Bearer {TODOIST_API_TOKEN}"},
                json={"content": text},
            )

        if response.status_code == 200:
            await processing_msg.edit_text(f"✅ Задача добавлена в Todoist:\n\n📝 {text}")
            logger.info(f"Задача создана: {text}")
        else:
            await processing_msg.edit_text(f"❌ Ошибка добавления задачи: {response.text}")
            logger.error(f"Ошибка Todoist API: {response.status_code} - {response.text}")

    except sr.UnknownValueError:
        await update.message.reply_text("🤷 Не удалось распознать текст. Попробуйте ещё раз.")
    except sr.RequestError as e:
        await update.message.reply_text(f"⚠️ Ошибка сервиса распознавания: {e}")
        logger.error(f"Ошибка Speech Recognition: {e}")
    except Exception as e:
        await update.message.reply_text(f"❌ Произошла ошибка: {e}")
        logger.error(f"Неожиданная ошибка: {e}")
    finally:
        # Гарантированное удаление временных файлов
        for path in [file_path, wav_path]:
            if os.path.exists(path):
                os.remove(path)


def main() -> None:
    """Запуск бота"""
    app = Application.builder().token(BOT_TOKEN).connect_timeout(30).read_timeout(30).build()

    # Обработчики
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", get_chat_id))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    logger.info("Бот запущен!")
    app.run_polling()


if __name__ == '__main__':
    main()
