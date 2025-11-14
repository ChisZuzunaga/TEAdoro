# app.py
# Backend: STT local (Whisper) + LLM OpenAI + TTS OpenAI
# Ejecuta con:
#   uvicorn app:app --host 0.0.0.0 --port 8000

import io, os, math, struct, tempfile, time

from fastapi import FastAPI, Request
from fastapi.responses import Response
from pydub import AudioSegment
from dotenv import load_dotenv
from openai import OpenAI
import whisper

load_dotenv()
app = FastAPI()

# --- Clientes / modelos ---
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Usa "tiny" o "base" para que vaya más rápido
WHISPER_MODEL_NAME = os.getenv("WHISPER_MODEL", "base")
whisper_model = whisper.load_model(WHISPER_MODEL_NAME)


def log_time(tag: str, t0: float) -> float:
    tnow = time.time()
    print(f"[TIME] {tag}: {tnow - t0:.3f}s")
    return tnow


@app.get("/ping")
def ping():
    return {"ok": True, "msg": "pong"}


@app.get("/tone")
def tone():
    """Genera un WAV 16 kHz / 16-bit mono de 1 kHz por 1 segundo (para validar salida ESP32)."""
    sr = 16000
    dur = 1.0
    N = int(sr * dur)
    pcm = bytearray()
    for n in range(N):
        s = int(30000 * math.sin(2 * math.pi * 1000 * n / sr))
        pcm += struct.pack("<h", s)

    hdr = bytearray(44)

    def put32(off, v): hdr[off:off+4] = struct.pack("<I", v)
    def put16(off, v): hdr[off:off+2] = struct.pack("<H", v)

    dataSize = len(pcm)
    hdr[0:4] = b"RIFF"; put32(4, 36 + dataSize); hdr[8:12] = b"WAVE"
    hdr[12:16] = b"fmt "; put32(16, 16); put16(20, 1); put16(22, 1); put32(24, sr)
    put32(28, sr * 2); put16(32, 2); put16(34, 16); hdr[36:40] = b"data"; put32(40, dataSize)

    return Response(content=bytes(hdr) + bytes(pcm), media_type="audio/wav")


@app.post("/api/ptt")
async def ptt(request: Request):
    """
    Flujo:
      1) Recibe WAV 16k/16-bit mono
      2) Normaliza con pydub
      3) STT local (Whisper)
      4) LLM (OpenAI)
      5) TTS (OpenAI) -> se devuelve WAV 16k/16-bit/mono
    """
    t0 = time.time()
    wav_bytes = await request.body()
    print(f"[ptt] RX {len(wav_bytes)} bytes")
    t1 = log_time("RX body", t0)

    # 1) Normalizar WAV
    try:
        audio = AudioSegment.from_file(io.BytesIO(wav_bytes), format="wav")
        audio = audio.set_channels(1).set_frame_rate(16000).set_sample_width(2)
        buf_wav = io.BytesIO()
        audio.export(buf_wav, format="wav")
        buf_wav.seek(0)
        print("[ptt] WAV normalizado OK")
    except Exception as e:
        msg = f"Error WAV: {e}"
        print("[ptt]", msg)
        return Response(content=msg, status_code=400)

    t2 = log_time("normalize", t1)

    # 2) STT local con Whisper
    try:
        # En Windows, NamedTemporaryFile debe usarse con delete=False y cerrar antes
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".wav")
        try:
            with os.fdopen(tmp_fd, "wb") as tmp:
                tmp.write(buf_wav.getvalue())
                tmp.flush()
            # Ahora el archivo está cerrado -> ffmpeg/whisper lo pueden abrir
            result = whisper_model.transcribe(tmp_path, language="es")
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        user_text = (result.get("text") or "").strip()
        print(f"[ptt] STT local: {user_text!r}")
    except Exception as e:
        msg = f"Error STT local (Whisper): {e}"
        print("[ptt]", msg)
        return Response(content=msg, status_code=500)

    t3 = log_time("STT local", t2)

    # 3) LLM (texto -> respuesta)
    try:
        resp = client.responses.create(
            model="gpt-4o-mini",
            input=[
                {
                    "role": "system",
                    "content": "Eres un asistente conversacional amable que siempre responde en español neutro y termina sus frases en PAPU."
                },
                {
                    "role": "user",
                    "content": user_text or "No entendí nada, responde algo genérico."
                }
            ],
            temperature=0.3,
            max_output_tokens=40,
        )
        ai_text = getattr(resp, "output_text", None) or (
            resp.get("output_text") if isinstance(resp, dict) else ""
        )
        ai_text = ai_text.strip()
        print(f"[ptt] LLM: {ai_text!r}")
    except Exception as e:
        msg = f"Error LLM: {e}"
        print("[ptt]", msg)
        return Response(content=msg, status_code=500)

    t4 = log_time("LLM", t3)

    # 4) TTS OpenAI → WAV 16k mono 16-bit
    try:
        tts = client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice="alloy",
            input=ai_text,
        )

        raw = None
        if isinstance(tts, (bytes, bytearray)):
            raw = bytes(tts)
        else:
            read = getattr(tts, "read", None)
            if callable(read):
                raw = read()
            elif isinstance(tts, dict) and "audio" in tts:
                raw = tts["audio"]
            else:
                try:
                    raw = bytes(tts)
                except Exception:
                    raw = None

        if not raw:
            raise RuntimeError("No se obtuvieron bytes de TTS")

        audio_tts = AudioSegment.from_file(io.BytesIO(raw))
        audio_tts = audio_tts.set_channels(1).set_frame_rate(16000).set_sample_width(2)
        out_buf = io.BytesIO()
        audio_tts.export(out_buf, format="wav")
        wav_out = out_buf.getvalue()

        print(f"[ptt] TTS->WAV bytes={len(wav_out)}")
    except Exception as e:
        msg = f"Error TTS: {e}"
        print("[ptt]", msg)
        return Response(content=msg, status_code=500)

    t5 = log_time("TTS", t4)
    log_time("TOTAL", t0)

    return Response(content=wav_out, media_type="audio/wav")
