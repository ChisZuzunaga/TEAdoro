# app.py
# pip install fastapi uvicorn python-multipart pydub openai python-dotenv
import io, os, math, struct
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydub import AudioSegment
from openai import OpenAI
from dotenv import load_dotenv
load_dotenv()   # carga .env en os.environ

app = FastAPI()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

@app.get("/ping")
def ping():
    return {"ok": True, "msg": "pong"}

@app.get("/tone")
def tone():
    """Genera un WAV 16 kHz / 16-bit mono de 1 kHz por 1 segundo (para validar salida ESP32)."""
    sr = 16000
    dur = 1.0
    N = int(sr*dur)
    pcm = bytearray()
    for n in range(N):
        s = int(30000 * math.sin(2*math.pi*1000*n/sr))  # 1 kHz
        pcm += struct.pack("<h", s)
    # WAV header
    hdr = bytearray(44)
    def put32(off, v): hdr[off:off+4] = struct.pack("<I", v)
    def put16(off, v): hdr[off:off+2] = struct.pack("<H", v)
    dataSize = len(pcm)
    hdr[0:4] = b"RIFF"; put32(4, 36+dataSize); hdr[8:12] = b"WAVE"
    hdr[12:16] = b"fmt "; put32(16, 16); put16(20, 1); put16(22, 1); put32(24, sr)
    put32(28, sr*2); put16(32, 2); put16(34, 16); hdr[36:40] = b"data"; put32(40, dataSize)
    return Response(content=bytes(hdr)+bytes(pcm), media_type="audio/wav")

@app.post("/api/ptt-echo")
async def ptt_echo(request: Request):
    """Devuelve exactamente el WAV recibido (ideal para probar ruta completa sin IA)."""
    data = await request.body()
    print(f"[ptt-echo] RX {len(data)} bytes")
    return Response(content=data, media_type="audio/wav")

@app.post("/api/ptt")
async def ptt(request: Request):
    """
    Pipeline completo:
      1) Recibe WAV 16k/16-bit mono
      2) STT -> texto (gpt-4o-mini-transcribe)
      3) LLM -> respuesta (gpt-4o-mini)
      4) TTS -> genera audio (gpt-4o-mini-tts) y lo convierte a WAV 16k/16-bit/mono
    """
    wav_bytes = await request.body()
    print(f"[ptt] RX {len(wav_bytes)} bytes")

    # 1) Normalizar el WAV recibido
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

    # 2) Transcripción (Speech-to-Text)
    try:
        stt = client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=("audio.wav", buf_wav, "audio/wav"),
            response_format="json",
            temperature=0.0,
            language="es"
        )
        user_text = getattr(stt, "text", None) or (stt.get("text") if isinstance(stt, dict) else "")
        print(f"[ptt] STT: {user_text!r}")
    except Exception as e:
        msg = f"Error STT: {e}"
        print("[ptt]", msg)
        return Response(content=msg, status_code=500)

    # 3) Generación de respuesta (LLM)
    try:
        resp = client.responses.create(
            model="gpt-4o-mini",
            input=[
                {"role": "system",
                 "content": "Eres un asistente conversacional amable que siempre responde en español neutro y termina sus frases en PAPU."},
                {"role": "user", "content": user_text}
            ],
            temperature=0.3
        )
        ai_text = getattr(resp, "output_text", None) or (resp.get("output_text") if isinstance(resp, dict) else "")
        print(f"[ptt] LLM: {ai_text!r}")
    except Exception as e:
        msg = f"Error LLM: {e}"
        print("[ptt]", msg)
        return Response(content=msg, status_code=500)

    # 4) Texto a voz (TTS) + conversión garantizada a WAV
    try:
        tts = client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice="alloy",
            input=ai_text
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

        # Convertir a WAV 16 kHz / 16 bit / mono con pydub
        src = io.BytesIO(raw)
        audio_tts = AudioSegment.from_file(src)
        audio_tts = audio_tts.set_channels(1).set_frame_rate(16000).set_sample_width(2)
        out_buf = io.BytesIO()
        audio_tts.export(out_buf, format="wav")
        wav_bytes = out_buf.getvalue()

        print(f"[ptt] TTS convertido a WAV: {len(wav_bytes)} bytes")
        return Response(content=wav_bytes, media_type="audio/wav")

    except Exception as e:
        msg = f"Error TTS: {e}"
        print("[ptt]", msg)
        return Response(content=msg, status_code=500)
