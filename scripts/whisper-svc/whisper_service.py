import os, numpy as np
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from faster_whisper import WhisperModel

MODEL   = os.getenv("WHISPER_MODEL", "small.en")
DEVICE  = os.getenv("WHISPER_DEVICE", "cuda")
COMPUTE = os.getenv("WHISPER_COMPUTE", "float16")
TOKEN   = os.getenv("WHISPER_TOKEN", "change-me")

print(f"[whisper] loading {MODEL} on {DEVICE}/{COMPUTE}...", flush=True)
model = WhisperModel(MODEL, device=DEVICE, compute_type=COMPUTE)
print("[whisper] ready", flush=True)

app = FastAPI()


@app.get("/health")
def health():
    return {"ok": True, "model": MODEL, "device": DEVICE}


@app.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    sample_rate: int = 16000,
    authorization: str = Header(default=""),
):
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, "bad token")
    raw = await audio.read()
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if sample_rate != 16000:
        raise HTTPException(400, "expected 16kHz")
    segments, info = model.transcribe(
        pcm,
        language="en",
        beam_size=1,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400},
        condition_on_previous_text=False,
        no_speech_threshold=0.55,
    )
    text = " ".join(s.text.strip() for s in segments).strip()
    return {
        "text": text,
        "duration": info.duration,
        "lang_prob": info.language_probability,
    }
