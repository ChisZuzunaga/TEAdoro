# app.py
# pip install fastapi uvicorn python-multipart pydub openai python-dotenv
import io, os, math, struct
from fastapi import FastAPI, Request
from fastapi.responses import Response
from pydub import AudioSegment
from pydub.silence import detect_nonsilent
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
app = FastAPI()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

@app.get("/ping")
def ping():
    return {"ok": True, "masassasg": "pong"}

@app.get("/tone")
def tone():
    """WAV 1kHz, 1s, 16kHz/16-bit mono para probar reproducción."""
    sr = 16000
    dur = 1.0
    N = int(sr * dur)
    pcm = bytearray()
    for n in range(N):
        s = int(30000 * math.sin(2*math.pi*1000*n/sr))
        pcm += struct.pack("<h", s)
    hdr = bytearray(44)
    def put32(off, v): hdr[off:off+4] = struct.pack("<I", v)
    def put16(off, v): hdr[off:off+2] = struct.pack("<H", v)
    dataSize = len(pcm)
    hdr[0:4] = b"RIFF"; put32(4, 36+dataSize); hdr[8:12] = b"WAVE"
    hdr[12:16] = b"fmt "; put32(16, 16); put16(20, 1); put16(22, 1); put32(24, sr)
    put32(28, sr*2); put16(32, 2); put16(34, 16); hdr[36:40] = b"data"; put32(40, dataSize)
    return Response(content=bytes(hdr)+bytes(pcm), media_type="audio/wav")

def trim_silence_wav_bytes(wav_bytes: bytes, thresh_db=-35.0, min_sil_ms=160) -> bytes:
    """Recorta silencios de borde para bajar latencia total (opcional)."""
    try:
        audio = AudioSegment.from_file(io.BytesIO(wav_bytes), format="wav")\
                            .set_channels(1).set_frame_rate(16000).set_sample_width(2)
        ns = detect_nonsilent(audio, min_silence_len=min_sil_ms, silence_thresh=thresh_db)
        if not ns:
            return wav_bytes
        start, end = ns[0][0], ns[-1][1]
        cropped = audio[start:end]
        buf = io.BytesIO()
        cropped.export(buf, format="wav")
        return buf.getvalue()
    except Exception:
        return wav_bytes  # si falla, sigue con original

@app.post("/api/ptt-echo")
async def ptt_echo(request: Request):
    data = await request.body()
    return Response(content=data, media_type="audio/wav")

@app.post("/api/ptt")
async def ptt(request: Request):
    """
    1) Recibe WAV 16k/16-bit mono
    2) (Opcional) recorta silencios de borde
    3) STT -> texto (gpt-4o-mini-transcribe)
    4) LLM -> respuesta corta (gpt-4o-mini)  (baja latencia)
    5) TTS -> MP3 (usualmente) -> WAV 16k/16-bit/mono
    """
    wav_bytes = await request.body()
    print(f"[ptt] RX {len(wav_bytes)} bytes")

    # 1.5) Recorte de silencios (opcional, ayuda a bajar tiempos)
    wav_bytes = trim_silence_wav_bytes(wav_bytes, thresh_db=-35.0, min_sil_ms=160)
    buf_wav = io.BytesIO(wav_bytes)  # ¡sin normalización costosa!

    # 2) STT
    try:
        stt = client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=("audio.wav", buf_wav, "audio/wav"),
            response_format="json",
            temperature=0.0,
            language="es",
        )
        user_text = getattr(stt, "text", None) or (stt.get("text") if isinstance(stt, dict) else "")
        print(f"[ptt] STT: {user_text!r}")
    except Exception as e:
        msg = f"Error STT: {e}"
        print("[ptt]", msg)
        return Response(content=msg, status_code=500)

    # 3) LLM (respuesta breve para que el TTS sea corto)
    try:
        resp = client.responses.create(
            model="gpt-4o-mini",
            input=[
                {"role": "system",
                 "content": "Responde en un lenguaje neutral."},
                {"role": "user", "content": user_text}
            ],
            temperature=0.2,
            max_output_tokens=50,
        )
        ai_text = getattr(resp, "output_text", None) or (resp.get("output_text") if isinstance(resp, dict) else "")
        print(f"[ptt] LLM: {ai_text!r}")
    except Exception as e:
        msg = f"Error LLM: {e}"
        print("[ptt]", msg)
        return Response(content=msg, status_code=500)

    # 4) TTS -> convertir a WAV 16k/16-bit/mono
    try:
        tts = client.audio.speech.create(model="gpt-4o-mini-tts", voice="alloy", input=ai_text)

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
            return Response(content="No audio from TTS", status_code=500)

        src = io.BytesIO(raw)
        audio_tts = AudioSegment.from_file(src)  # pydub detecta mp3/aac, etc.
        audio_tts = audio_tts.set_channels(1).set_frame_rate(16000).set_sample_width(2)
        out_buf = io.BytesIO()
        audio_tts.export(out_buf, format="wav")
        wav_out = out_buf.getvalue()
        print(f"[ptt] TTS->WAV: {len(wav_out)} bytes")
        return Response(content=wav_out, media_type="audio/wav")

    except Exception as e:
        msg = f"Error TTS: {e}"
        print("[ptt]", msg)
        return Response(content=msg, status_code=500)
