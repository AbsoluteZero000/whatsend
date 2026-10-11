import {createServer} from 'node:http';
import {timingSafeEqual} from 'node:crypto';
import {pathToFileURL} from 'node:url';
import Busboy from 'busboy';
import {WhatsAppManager, ClientError, isSessionId} from './manager.mjs';
import {mediaKind} from './media.mjs';

async function parseBody(request) {
  if (request.headers['content-type']?.startsWith('multipart/form-data')) {
    return new Promise((resolve, reject) => {
      const parser = Busboy({headers: request.headers, defParamCharset: 'utf8', limits: {files: 1, fields: 3, fileSize: 50 * 1024 * 1024, fieldSize: 4096}});
      const fields = {};
      let media, failed = false;
      const fail = () => { failed = true; reject(new ClientError(400, 'invalid_media', 'Invalid attachment or upload limit exceeded.')); };
      parser.on('field', (name, value, info) => { if (info.valueTruncated) fail(); fields[name] = value; });
      parser.on('file', (name, file, info) => {
        const chunks = [];
        if (name !== 'file') fail();
        file.on('data', chunk => chunks.push(chunk));
        file.on('limit', fail);
        file.on('end', () => { media = {bytes: Buffer.concat(chunks), filename: info.filename, mime: info.mimeType}; });
      });
      parser.on('filesLimit', fail); parser.on('fieldsLimit', fail); parser.on('error', fail);
      parser.on('close', () => {
        if (failed) return;
        if (!media?.bytes.length) return fail();
        media.kind = mediaKind(media);
        if (!media.kind) return fail();
        if (Buffer.byteLength(fields.caption || '') > 1024) return fail();
        resolve({recipient: fields.recipient, caption: fields.caption || '', media});
      });
      request.pipe(parser);
    });
  }
  const chunks = []; let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 16384) throw new ClientError(413, 'too_large', 'Request exceeds upload limit.');
    chunks.push(chunk);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString()); }
  catch { throw new ClientError(400, 'invalid_json', 'Invalid request body.'); }
}

export function workerServer(manager, secret) {
  if (!secret) throw new Error('Internal service secret is required');
  return createServer(async (request, response) => {
    const reply = (status, payload) => {
      response.writeHead(status, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'});
      response.end(JSON.stringify(payload));
    };
    try {
      const received = Buffer.from(request.headers.authorization || '');
      const expected = Buffer.from('Bearer ' + secret);
      if (received.length !== expected.length || !timingSafeEqual(received, expected)) throw new ClientError(401, 'unauthorized', 'Authentication required.');
      const url = new URL(request.url, 'http://localhost');
      if (request.method === 'GET' && url.pathname === '/health') return reply(200, {data: {status: 'ready'}});
      const match = /^\/api\/v1\/sessions\/([^/]+)(?:\/(connect|disconnect|status|qr|groups|messages)(?:\/([^/]+))?)?$/.exec(url.pathname);
      if (!match || !isSessionId(match[1])) throw new ClientError(404, 'not_found', 'Not found.');
      const [, id, action, messageId] = match;
      if (request.method === 'DELETE' && !action) { await manager.remove(id); return reply(200, {data: {id}}); }
      if (request.method === 'POST' && action === 'connect') return reply(200, {data: await manager.connect(id)});
      if (request.method === 'POST' && action === 'disconnect') return reply(200, {data: await manager.logout(id)});
      if (request.method === 'GET' && action === 'status') return reply(200, {data: manager.status(id)});
      if (request.method === 'GET' && action === 'qr') {
        const qr = manager.qr(id);
        if (!qr) throw new ClientError(409, 'qr_not_ready', 'QR code is still being prepared. Refresh the connection status.');
        return reply(200, {data: qr});
      }
      if (request.method === 'GET' && action === 'groups') return reply(200, {data: await manager.groups(id), next_cursor: ''});
      if (request.method === 'GET' && action === 'messages' && isSessionId(messageId || '')) return reply(200, {data: manager.message(id, messageId)});
      if (request.method === 'POST' && action === 'messages' && !messageId) {
        const payload = await parseBody(request);
        if (!payload.media && (typeof payload.text !== 'string' || !payload.text || Buffer.byteLength(payload.text) > 4096)) throw new ClientError(400, 'invalid_text', 'Message must contain 1–4096 UTF-8 bytes.');
        return reply(202, {data: manager.enqueue(id, request.headers['idempotency-key'], payload)});
      }
      throw new ClientError(404, 'not_found', 'Not found.');
    } catch (error) {
      if (!response.headersSent) reply(error instanceof ClientError ? error.status : 502, {error: {
        code: error instanceof ClientError ? error.code : 'whatsapp_unavailable',
        message: error instanceof ClientError ? error.message : 'WhatsApp could not complete this operation. Try reconnecting.',
      }});
    }
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const manager = new WhatsAppManager({root: process.env.WHATSAPP_DATA_DIR || '/data/whatsapp', storageKey: process.env.WHATSAPP_STORAGE_KEY});
  const server = workerServer(manager, process.env.WHATSAPP_WORKER_SECRET);
  server.listen(Number(process.env.WHATSAPP_WORKER_PORT || 3001), '127.0.0.1', () => {
    process.stdout.write('Built-in WhatsApp service ready\n');
    void manager.restore();
  });
  let closing = false;
  async function shutdown() {
    if (closing) return; closing = true;
    server.close(); await manager.close(); process.exit(0);
  }
  process.on('SIGTERM', shutdown); process.on('SIGINT', shutdown);
}
