# app/main.py
import os
import tempfile
from io import BytesIO
from typing import Optional

from fastapi import (
    FastAPI,
    File,
    UploadFile,
    Form,
    HTTPException,
    Header,
    BackgroundTasks,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from elevenlabs.client import ElevenLabs

# Настройка через env
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
# "Вечный" ключ доступа для этого endpoint (должен быть длинной случайной строкой)
BACKEND_ACCESS_KEY = os.getenv("BACKEND_ACCESS_KEY")

if ELEVENLABS_API_KEY is None:
    raise RuntimeError("ELEVENLABS_API_KEY не задан в окружении")
if BACKEND_ACCESS_KEY is None:
    # можно позволить запускать локально без ключа, но лучше требовать
    raise RuntimeError("BACKEND_ACCESS_KEY не задан в окружении")

elevenlabs = ElevenLabs(api_key=ELEVENLABS_API_KEY)
# формат выхода: используем mp3 для удобства (совместим с большинством клиентов)
OUTPUT_FORMAT = "mp3_44100_128"

app = FastAPI(title="ElevenLabs voice clone TTS backend")


def _save_upload_tempfile(upload: UploadFile) -> str:
    """Сохранить UploadFile во временный файл и вернуть путь."""
    suffix = os.path.splitext(upload.filename)[1] or ".wav"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix="ref_")
    try:
        # читаем порциями, чтобы не держать весь файл в памяти
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            tmp.write(chunk)
    finally:
        tmp.close()
    return tmp.name


def _remove_file(path: Optional[str]):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def create_voice_from_reference(clone_name: str, ref_path: str):
    """
    Создаёт voice clone через ElevenLabs IVC.
    Возвращает объект voice (как в tasks.py) или выбрасывает исключение.
    """
    # SDK: ожидает файловую структуру. tasks.py использует BytesIO(open(...).read())
    with open(ref_path, "rb") as f:
        audio_bytes = BytesIO(f.read())
    voice = elevenlabs.voices.ivc.create(name=clone_name, files=[audio_bytes])
    return voice


def synthesize_text_to_file(voice_id: str, text: str, out_path: str):
    """
    Генерирует аудио (итератор чанков) и записывает в out_path.
    Возвращает out_path при успехе.
    """
    response = elevenlabs.text_to_speech.convert(
        text=text,
        voice_id=voice_id,
        model_id="eleven_multilingual_v2",
        output_format=OUTPUT_FORMAT,
    )

    # response — итератор/генератор чанков байтов
    with open(out_path, "wb") as f:
        for chunk in response:
            if chunk:
                f.write(chunk)
    # проверим, что вышло что-то весомое
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 100:
        raise RuntimeError("Synthesized file is empty or too small")
    return out_path


async def _auth_header(x_api_key: str = Header(...)):
    """Депенденси для проверки ключа — бросает 401 если неверный"""
    if x_api_key != BACKEND_ACCESS_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return x_api_key


@app.post("/synthesize", summary="Synthesize voice from reference audio + text")
async def synthesize_endpoint(
    background_tasks: BackgroundTasks,
    ref_audio: UploadFile = File(..., description="Reference audio file (wav/mp3/ogg)"),
    text: str = Form(..., description="Text to synthesize"),
    x_api_key: str = Header(..., alias="X-API-KEY"),
):
    """
    POST /synthesize
    Form fields:
      - ref_audio: file upload (audio reference)
      - text: text to generate
    Header:
      - X-API-KEY: your eternal access key
    Returns: generated audio (mp3) as response body
    """

    # авторизация
    if x_api_key != BACKEND_ACCESS_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # лимит размера загружаемого файла (защита): 10 MB по умолчанию
    MAX_REF_BYTES = int(os.getenv("MAX_REF_BYTES", 10 * 1024 * 1024))
    # проверка content-length если задан (не обязателен)
    # but UploadFile is already accepted — check file size while reading
    # Сохраняем референс
    try:
        ref_path = _save_upload_tempfile(ref_audio)
        # защита: проверим размер файла
        if os.path.getsize(ref_path) > MAX_REF_BYTES:
            _remove_file(ref_path)
            raise HTTPException(status_code=400, detail="Reference file too large")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save reference: {e}")

    # создаём временный файл для результата
    out_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3", prefix="out_")
    out_path = out_tmp.name
    out_tmp.close()

    # планируем удалить временные файлы в background
    background_tasks.add_task(_remove_file, ref_path)
    background_tasks.add_task(_remove_file, out_path)

    try:
        # CREATE VOICE (IVC)
        # Здесь можно использовать уникальное имя (например hash user/key/timestamp)
        clone_name = f"ref_clone_{os.path.basename(ref_path)}"
        voice = create_voice_from_reference(clone_name, ref_path)
        # синтез
        synthesize_text_to_file(voice.voice_id, text, out_path)

        # Отдаём файл как stream
        file_like = open(out_path, "rb")
        headers = {"Content-Disposition": f'attachment; filename="tts.mp3"'}
        return StreamingResponse(file_like, media_type="audio/mpeg", headers=headers)
    except Exception as e:
        # при ошибке удалим результат (background_tasks его тоже удалит)
        raise HTTPException(status_code=500, detail=f"TTS error: {e}")
