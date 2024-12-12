from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
import requests
import os
from pydub import AudioSegment
import speech_recognition as sr

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN')  # Токен передадим через Render

# Функция для обработки голосовых сообщений
def handle_voice(update: Update, context: CallbackContext) -> None:
    voice_file = update.message.voice.get_file()
    file_path = "voice.ogg"

    # Скачиваем аудио
    voice_file.download(file_path)

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
            update.message.reply_text(f"Распознанный текст: {text}")
        except sr.UnknownValueError:
            update.message.reply_text("Не удалось распознать текст.")
        except sr.RequestError as e:
            update.message.reply_text(f"Ошибка распознавания: {e}")

    # Удаляем временные файлы
    os.remove(file_path)
    os.remove(wav_path)

# Запуск бота
def main() -> None:
    updater = Updater(BOT_TOKEN)
    dispatcher = updater.dispatcher

    # Обработчики
    dispatcher.add_handler(MessageHandler(Filters.voice, handle_voice))

    # Запуск
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()