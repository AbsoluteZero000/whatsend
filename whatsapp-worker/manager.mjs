import makeWASocket, {BufferJSON, initAuthCreds, proto, DisconnectReason, Browsers, generateMessageIDV2} from '@whiskeysockets/baileys';
import QRCode from 'qrcode';
import pino from 'pino';
import {randomUUID, createHash} from 'node:crypto';
import {join} from 'node:path';
import {mkdirSync, readdirSync, rmSync} from 'node:fs';
import {EncryptedStore} from './store.mjs';

export class ClientError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}
export const isSessionId = id => /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id);
export function recipientJid(value) {
  if (typeof value !== 'string') throw new ClientError(400, 'invalid_recipient', 'Enter a phone number or WhatsApp group ID.');
  if (/^\+?[0-9]{7,15}$/.test(value)) return value.replace(/^\+/, '') + '@s.whatsapp.net';
  if (/^[0-9]+(?:-[0-9]+)?@g\.us$/.test(value) || /^[0-9]+@(s\.whatsapp\.net|lid)$/.test(value)) return value;
  throw new ClientError(400, 'invalid_recipient', 'Use an international phone number or a WhatsApp group ID ending in @g.us.');
}

export class WhatsAppManager {
  constructor({root, storageKey, socketFactory = makeWASocket}) {
    this.root = root;
    this.store = new EncryptedStore(storageKey, BufferJSON.replacer, BufferJSON.reviver);
    this.socketFactory = socketFactory;
    this.sessions = new Map();
    mkdirSync(root, {recursive: true, mode: 0o700});
  }
  session(id) {
    if (!isSessionId(id)) throw new ClientError(400, 'invalid_session', 'Invalid WhatsApp connection.');
    if (this.sessions.has(id)) return this.sessions.get(id);
    const directory = join(this.root, id);
    const auth = this.store.read(join(directory, 'auth.enc'), {creds: initAuthCreds(), keys: {}});
    const journal = this.store.read(join(directory, 'messages.enc'), {});
    // A process may have died after writing to WhatsApp. Never replay those sends.
    for (const operation of Object.values(journal)) {
      if (operation.status === 'processing') {
        operation.status = 'unknown'; operation.error = 'Sending was interrupted. Check WhatsApp before sending again.';
      }
    }
    const session = {id, directory, auth, journal, state: 'disconnected', socket: null, qr: null,
                     stopped: false, generation: 0, reconnectCount: 0, running: false, connecting: null};
    this.sessions.set(id, session);
    this.saveJournal(session);
    return session;
  }
  saveAuth(session) { this.store.write(join(session.directory, 'auth.enc'), session.auth); }
  saveJournal(session) { this.store.write(join(session.directory, 'messages.enc'), session.journal); }
  status(id) {
    const s = this.session(id);
    if (s.qr && Date.parse(s.qr.expires_at) <= Date.now()) { s.qr = null; if (s.state === 'qr_required') s.state = 'connecting'; }
    return {id, state: s.state, phone: s.socket?.user?.id?.split(':')[0]?.split('@')[0] || null};
  }
  async connect(id) {
    const s = this.session(id);
    if (s.connecting) { await s.connecting; return this.status(id); }
    if (s.socket && ['connected', 'connecting', 'qr_required', 'reconnecting'].includes(s.state)) return this.status(id);
    s.connecting = this.open(s);
    try { await s.connecting; } finally { s.connecting = null; }
    return this.status(id);
  }
  async open(s) {
    s.stopped = false;
    clearTimeout(s.reconnectTimer);
    s.state = s.auth.creds.me?.id ? 'reconnecting' : 'connecting';
    const generation = ++s.generation;
    s.socket?.end(new Error('Replacing WhatsApp connection'));
    const save = () => this.saveAuth(s);
    const socket = this.socketFactory({
      auth: {creds: s.auth.creds, keys: {
        get: async (type, ids) => Object.fromEntries(ids.filter(id => s.auth.keys[type]?.[id]).map(id => [id,
          type === 'app-state-sync-key' ? proto.Message.AppStateSyncKeyData.fromObject(s.auth.keys[type][id]) : s.auth.keys[type][id]])),
        set: async data => {
          if (generation !== s.generation) return;
          for (const [type, entries] of Object.entries(data)) {
            s.auth.keys[type] ||= {};
            for (const [id, value] of Object.entries(entries)) {
              if (value === null) delete s.auth.keys[type][id]; else s.auth.keys[type][id] = value;
            }
          }
          save();
        },
      }},
      logger: pino({level: 'silent'}), browser: Browsers.ubuntu('WhatSend'),
      syncFullHistory: false, markOnlineOnConnect: false,
      getMessage: async () => undefined,
    });
    s.socket = socket;
    // Baileys reports negative server ACKs through messages.update, but does
    // not emit successful server ACKs there. sendMessage only writes the
    // stanza; its returned PENDING status is not a server acknowledgement.
    socket.ws?.on('CB:ack,class:message', ({attrs}) => {
      if (generation !== s.generation || !attrs?.id) return;
      const operation = Object.values(s.journal).find(o => o.waId === attrs.id);
      if (!operation || operation.status === 'sent') return;
      operation.status = attrs.error ? 'failed' : 'sent';
      if (attrs.error) operation.error = `WhatsApp rejected this message (${attrs.error}).`;
      else delete operation.error;
      this.saveJournal(s);
    });
    socket.ev.on('creds.update', data => { if (generation === s.generation) { Object.assign(s.auth.creds, data); save(); } });
    socket.ev.on('connection.update', update => {
      void this.connectionUpdate(s, generation, update).catch(() => {
        if (generation === s.generation) { s.state = 'failed'; s.qr = null; }
      });
    });
    socket.ev.on('messages.update', updates => {
      if (generation !== s.generation) return;
      for (const {key, update} of updates) {
        if (update.status === undefined) continue;
        const operation = Object.values(s.journal).find(o => o.waId === key.id);
        if (!operation || operation.status === 'sent') continue;
        if (update.status >= proto.WebMessageInfo.Status.SERVER_ACK) { operation.status = 'sent'; delete operation.error; }
        else if (update.status === proto.WebMessageInfo.Status.ERROR) operation.status = 'failed';
      }
      this.saveJournal(s);
    });
  }
  async connectionUpdate(s, generation, update) {
    if (generation !== s.generation) return;
    if (update.qr) {
      const png = await QRCode.toBuffer(update.qr, {type: 'png', width: 320, margin: 2});
      if (generation !== s.generation || s.state === 'connected' || s.stopped) return;
      s.qr = {png_base64: png.toString('base64'), expires_at: new Date(Date.now() + 45000).toISOString()};
      s.state = 'qr_required';
    }
    if (update.connection === 'open') {
      s.state = 'connected'; s.qr = null; s.reconnectCount = 0;
      this.saveAuth(s);
      void this.drain(s);
    }
    if (update.connection === 'close') {
      s.socket = null; s.qr = null;
      if (s.stopped) { s.state = 'disconnected'; return; }
      const code = update.lastDisconnect?.error?.output?.statusCode;
      if (code === DisconnectReason.loggedOut || code === DisconnectReason.badSession || code === DisconnectReason.connectionReplaced) {
        s.state = 'disconnected'; s.stopped = true;
        if (code !== DisconnectReason.connectionReplaced) {
          s.auth = {creds: initAuthCreds(), keys: {}}; this.saveAuth(s);
        }
        return;
      }
      s.state = 'reconnecting';
      const delay = Math.min(30000, 1000 * 2 ** Math.min(s.reconnectCount++, 5));
      s.reconnectTimer = setTimeout(() => { if (!s.stopped) void this.connect(s.id).catch(() => { s.state = 'failed'; }); }, delay);
    }
  }
  qr(id) { const s = this.session(id); this.status(id); return s.qr; }
  async logout(id) {
    const s = this.session(id);
    s.stopped = true; clearTimeout(s.reconnectTimer); ++s.generation;
    try { if (s.socket?.user) await s.socket.logout(); } finally {
      s.socket?.end(new Error('Logged out')); s.socket = null; s.qr = null; s.state = 'disconnected';
      s.auth = {creds: initAuthCreds(), keys: {}}; this.saveAuth(s);
      for (const o of Object.values(s.journal)) if (o.status === 'pending') { o.status = 'failed'; o.error = 'WhatsApp was logged out before sending.'; }
      this.saveJournal(s);
    }
    return this.status(id);
  }
  async remove(id) {
    await this.logout(id);
    this.sessions.delete(id);
    rmSync(join(this.root, id), {recursive: true, force: true});
  }
  async recipientGroups(socket, parent = null) {
    const groups = Object.fromEntries(Object.values(await socket.groupFetchAllParticipating()).map(g => [g.id, g]));
    // WhatsApp can return communities separately from ordinary groups.
    if (socket.communityFetchAllParticipating) {
      try {
        for (const community of Object.values(await socket.communityFetchAllParticipating())) {
          const existing = groups[community.id];
          groups[community.id] = {...existing, ...community,
            isCommunity: !!(existing?.isCommunity || community.isCommunity),
            isCommunityAnnounce: !!(existing?.isCommunityAnnounce || community.isCommunityAnnounce)};
        }
      } catch {
        console.warn('WhatsApp community list could not be loaded; ordinary groups remain available.');
      }
    }
    if (parent) groups[parent.id] = {...groups[parent.id], ...parent};
    // Some accounts omit the announcement chat from the participating list.
    // Inspect linked chats by their metadata, never by their name or ID order.
    if (socket.communityFetchLinkedGroups) {
      for (const community of Object.values(groups).filter(g => g.isCommunity && !g.isCommunityAnnounce)) {
        if (Object.values(groups).some(g => g.isCommunityAnnounce && g.linkedParent === community.id)) continue;
        try {
          const result = await socket.communityFetchLinkedGroups(community.id);
          for (const linked of result.linkedGroups || []) {
            if (!linked.id) continue;
            try {
              const metadata = await socket.groupMetadata(recipientJid(linked.id));
              if (metadata.isCommunityAnnounce && (!metadata.linkedParent || metadata.linkedParent === community.id)) {
                groups[metadata.id] = {...metadata, linkedParent: community.id};
              }
            } catch { /* Inaccessible linked chats cannot be selected. */ }
          }
        } catch {
          console.warn('WhatsApp linked announcement chats could not be loaded.');
        }
      }
    }
    return groups;
  }
  async groups(id) {
    const s = this.session(id);
    if (s.state !== 'connected') throw new ClientError(409, 'not_connected', 'Scan the WhatsApp QR code before loading groups.');
    const groups = await this.recipientGroups(s.socket);
    // Community parents are containers, not announcement chats. Only offer
    // their actual subgroups, and make announcement targets easy to identify.
    return Object.values(groups).filter(g => !g.isCommunity || g.isCommunityAnnounce).map(g => ({
      external_chat_id: g.id,
      name: g.isCommunityAnnounce
        ? `${groups[g.linkedParent]?.subject || g.subject || g.id} — Announcements`
        : g.linkedParent && groups[g.linkedParent]?.subject
          ? `${groups[g.linkedParent].subject} — ${g.subject || g.id}`
          : g.subject || g.id,
    }));
  }
  async resolveRecipient(socket, recipient) {
    if (!recipient.endsWith('@g.us')) return recipient;
    let metadata;
    try { metadata = await socket.groupMetadata(recipient); }
    catch (error) {
      if (!socket.communityMetadata) throw error;
      metadata = await socket.communityMetadata(recipient);
    }
    if (!metadata.isCommunity || metadata.isCommunityAnnounce) return recipient;
    // Existing jobs and manually entered IDs may still point at a parent.
    const groups = await this.recipientGroups(socket, metadata);
    const announcements = Object.values(groups).filter(g => g.isCommunityAnnounce && g.linkedParent === recipient);
    if (announcements.length !== 1) {
      throw new ClientError(400, 'announcement_not_found',
        'The community announcement group is unavailable. Join it with this WhatsApp account and select its announcement group.');
    }
    return recipientJid(announcements[0].id);
  }
  enqueue(id, key, payload) {
    const s = this.session(id);
    if (!key || key.length > 255 || !/^[\x21-\x7e]+$/.test(key)) throw new ClientError(400, 'invalid_idempotency_key', 'Idempotency key is required.');
    payload.recipient = recipientJid(payload.recipient);
    if (payload.media && Buffer.isBuffer(payload.media.bytes)) payload.media.bytes = payload.media.bytes.toString('base64');
    const hash = createHash('sha256').update(JSON.stringify(payload)).digest('hex');
    const existing = Object.values(s.journal).find(o => o.callerKey === key);
    if (existing) {
      if (existing.payloadHash !== hash) throw new ClientError(409, 'idempotency_conflict', 'Idempotency key has another payload.');
      return {id: existing.id, status: 'pending'};
    }
    if (s.state !== 'connected') throw new ClientError(409, 'not_connected', 'Scan the WhatsApp QR code before sending messages.');
    const operation = {id: randomUUID(), callerKey: key, payloadHash: hash, payload, status: 'pending', createdAt: Date.now()};
    s.journal[operation.id] = operation;
    this.saveJournal(s);
    setImmediate(() => { void this.drain(s); });
    return {id: operation.id, status: 'pending'};
  }
  message(id, messageId) {
    const session = this.session(id);
    const operation = session.journal[messageId];
    if (!operation) throw new ClientError(404, 'message_not_found', 'Message not found.');
    if (operation.status === 'processing' && Date.now() - operation.attemptedAt > 120000) {
      operation.status = 'unknown'; operation.error = 'WhatsApp did not acknowledge this send. Check WhatsApp before sending again.';
      this.saveJournal(session);
    }
    return {id: operation.id, status: operation.status, error: operation.error || null};
  }
  async drain(s) {
    if (s.running || s.state !== 'connected') return;
    s.running = true;
    try {
      for (const operation of Object.values(s.journal)) {
        if (s.state !== 'connected') break;
        if (operation.status !== 'pending') continue;
        const socket = s.socket;
        try {
          operation.resolvedRecipient = await this.resolveRecipient(socket, operation.payload.recipient);
        } catch (error) {
          // Resolution happens before any send, so this is a definite failure.
          operation.status = 'failed';
          operation.error = error instanceof ClientError ? error.message :
            'Could not load the WhatsApp group before sending. Check the connection and group membership.';
          if (operation.payload.media) delete operation.payload.media.bytes;
          this.saveJournal(s);
          continue;
        }
        // A reconnect/logout may have occurred while resolving the target.
        if (s.state !== 'connected' || s.socket !== socket) break;
        operation.status = 'processing';
        operation.attemptedAt = Date.now();
        operation.waId = generateMessageIDV2(s.socket.user?.id);
        this.saveJournal(s);
        try {
          const p = operation.payload;
          const content = p.media ? {[p.media.kind]: Buffer.from(p.media.bytes, 'base64'), caption: p.caption || '',
            mimetype: p.media.mime, fileName: p.media.filename} : {text: p.text};
          const response = await socket.sendMessage(operation.resolvedRecipient, content, {messageId: operation.waId});
          if (response?.status >= proto.WebMessageInfo.Status.SERVER_ACK) operation.status = 'sent';
        } catch {
          if (operation.status !== 'sent') {
            operation.status = 'unknown'; operation.error = 'Delivery acknowledgement is uncertain. Check WhatsApp before sending again.';
          }
        }
        // Media bodies are no longer needed once a send was attempted.
        if (operation.payload.media) delete operation.payload.media.bytes;
        this.saveJournal(s);
      }
    } finally {
      s.running = false;
      // Enqueue can run while sendMessage is awaiting WhatsApp. Those sends
      // were not in this batch's snapshot, and their drain calls saw running.
      if (s.state === 'connected' && Object.values(s.journal).some(o => o.status === 'pending')) {
        setImmediate(() => { void this.drain(s); });
      }
    }
  }
  async restore() {
    for (const id of readdirSync(this.root).filter(isSessionId)) {
      const s = this.session(id);
      // QR pairing stores `me`; `registered` is set by the pairing-code flow.
      if (s.auth.creds.me?.id) void this.connect(id).catch(() => { s.state = 'failed'; });
    }
  }
  async close() {
    for (const s of this.sessions.values()) {
      s.stopped = true; clearTimeout(s.reconnectTimer); ++s.generation;
      s.socket?.end(new Error('Service shutting down')); this.saveAuth(s);
    }
  }
}
