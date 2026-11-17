# The standalone WhatsApp worker, deployed on its own (fly.toml in this folder).
# Build from this folder: `fly deploy` here, or `docker build .`.
FROM node:22-bookworm-slim
WORKDIR /worker
COPY package*.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY . .
RUN mkdir -p /data/whatsapp && chown -R node:node /data /worker
USER node
ENV WHATSAPP_DATA_DIR=/data/whatsapp
EXPOSE 3001
CMD ["node", "server.mjs"]
