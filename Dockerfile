FROM python:3.12-slim

ARG HUGO_VERSION=0.168.0
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl jq git openssh-client ca-certificates \
    && curl -fsSL -o /tmp/hugo.deb \
        "https://github.com/gohugoio/hugo/releases/download/v${HUGO_VERSION}/hugo_extended_${HUGO_VERSION}_linux-amd64.deb" \
    && apt-get install -y /tmp/hugo.deb && rm /tmp/hugo.deb \
    && rm -rf /var/lib/apt/lists/*

# ssh (git push de vvmm) exige que el uid tenga usuario
RUN useradd -u 1000 -d /data -M -s /bin/sh app

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PYTHONUNBUFFERED=1 HOME=/data
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
