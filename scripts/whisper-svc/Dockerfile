FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
        python3 python3-pip ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir \
        faster-whisper==1.0.3 \
        fastapi==0.115.5 \
        uvicorn[standard]==0.32.1 \
        python-multipart==0.0.17 \
        requests \
        numpy

WORKDIR /app
COPY whisper_service.py /app/

ENV WHISPER_MODEL=small.en
ENV WHISPER_DEVICE=cuda
ENV WHISPER_COMPUTE=float16

EXPOSE 8000
CMD ["uvicorn", "whisper_service:app", "--host", "0.0.0.0", "--port", "8000"]
