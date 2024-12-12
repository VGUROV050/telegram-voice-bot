from telegram import Update
from telegram.ext import Application, MessageHandler, CallbackContext, filters
import os
from pydub import AudioSegment
import speech_recognition as sr

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN')  # Токен передадим через Render

# Функция для обработки голосовых сообщений
async def handle_voice(update: Update, context: CallbackContext) -> None:
    voice_file = await update.message.voice.get_file()
    file_path = "voice.ogg"

    # Скачиваем аудио
    await voice_file.download(file_path)

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
            await update.message.reply_text(f"Распознанный текст: {text}")
        except sr.UnknownValueError:
            await update.message.reply_text("Не удалось распознать текст.")
        except sr.RequestError as e:
            await update.message.reply_text(f"Ошибка распознавания: {e}")

    # Удаляем временные файлы
    os.remove(file_path)
    os.remove(wav_path)

# Запуск бота
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    # Обработчики
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # Запуск
    app.run_polling()

if __name__ == '__main__':
    main()