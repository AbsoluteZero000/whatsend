FROM node:22-bookworm-slim AS whatsapp
WORKDIR /worker
COPY whatsapp-worker/package*.json ./
RUN npm ci --omit=dev --no-audit --no-fund

FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 libatomic1 && rm -rf /var/lib/apt/lists/*
COPY --from=whatsapp /usr/local/bin/node /usr/local/bin/node

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=whatsapp /worker/node_modules ./whatsapp-worker/node_modules

RUN mkdir -p /data

EXPOSE 8000

ENV DATABASE_URL=sqlite+aiosqlite:////data/whatsend.db
ENV UPLOAD_DIR=/data/uploads

CMD ["python", "run.py"]
