from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackContext, filters
import os
from pydub import AudioSegment
import speech_recognition as sr
import requests  # Импорт requests

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN')  # Токен передадим через Render
# ID чата пользователя, которому нужно отправить текст
GROUP_CHAT_ID = -1002433054865  # Замените на реальный chat_id
TODOIST_API_TOKEN = os.getenv("TODOIST_API_TOKEN")

async def get_chat_id(update: Update, context: CallbackContext) -> None:
    chat_id = update.message.chat_id
    print(f"Chat ID: {chat_id}")  # Выводит chat_id группы в консоль
    await update.message.reply_text(f"Group Chat ID: {chat_id}")

# Функция для обработки голосовых сообщений
async def handle_voice(update: Update, context: CallbackContext) -> None:
    # Получение файла голосового сообщения
    voice_file = await update.message.voice.get_file()
    file_path = "voice.ogg"

    # Скачиваем аудио
    await voice_file.download_to_drive(file_path)

    # Конвертируем в WAV
    audio = AudioSegment.from_file(file_path)
    wav_path = "voice.wav"
    audio.export(wav_path, format="wav")

    # Распознаем текст
    recognizer = sr.Recognizer()
    with sr.AudioFile(wav_path) as source:
        audio_data = recognizer.record(source)
        try:
            text = recognizer.recognize_google(audio_data, language="ru-RU")
            # Формируем задачу для Todoist
            task_data = {
                "content": text,
            }

             # Отправляем задачу в Todoist
            response = requests.post(
                "https://api.todoist.com/rest/v2/tasks",
                headers={
                    "Authorization": f"Bearer {TODOIST_API_TOKEN}"
                },
                json=task_data,
            )

            if response.status_code == 200 or response.status_code == 204:
                await update.message.reply_text("Задача успешно добавлена в Todoist!")
            else:
                await update.message.reply_text(f"Ошибка добавления задачи: {response.text}")
            
            # Отправляем текст в группу
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=text)
        except sr.UnknownValueError:
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text="Не удалось распознать текст.")
        except sr.RequestError as e:
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=f"Ошибка распознавания: {e}")

    # Удаляем временные файлы
    os.remove(file_path)
    os.remove(wav_path)

# Запуск бота
def main() -> None:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    app = Application.builder().token(BOT_TOKEN).connect_timeout(30).read_timeout(30).build()

    # Обработчики
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # Запуск
    app.run_polling()

if __name__ == '__main__':
    main()