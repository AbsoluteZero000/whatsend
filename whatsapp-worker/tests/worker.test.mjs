import {test} from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {mkdtempSync, readFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {WhatsAppManager, recipientJid} from '../manager.mjs';
import {EncryptedStore} from '../store.mjs';
import {workerServer} from '../server.mjs';

const tick = () => new Promise(resolve => setTimeout(resolve, 25));
function setup(t) {
  const root = mkdtempSync(join(tmpdir(), 'whatsend-worker-'));
  const sockets = [], sends = [];
  const manager = new WhatsAppManager({root, storageKey: 'test-encryption-key', socketFactory: config => {
    const socket = {ev: new EventEmitter(), user: {id: '201234567890:1@s.whatsapp.net'},
      groupFetchAllParticipating: async () => ({group: {id: '123456789@g.us', subject: 'My group'}}),
      sendMessage: async (recipient, content, options) => { sends.push({recipient, content, options}); return {status: 1}; },
      logout: async () => {}, end: () => {}, config};
    sockets.push(socket); return socket;
  }});
  t.after(async () => { await manager.close(); rmSync(root, {recursive: true, force: true}); });
  return {manager, sockets, sends, root};
}

test('encrypted storage is confidential, authenticated, and preserves JSON', t => {
  const {root} = setup(t);
  const file = join(root, 'test.enc');
  const store = new EncryptedStore('key');
  store.write(file, {secret: 'phone-credentials'});
  assert.equal(readFileSync(file).includes(Buffer.from('phone-credentials')), false);
  assert.deepEqual(store.read(file), {secret: 'phone-credentials'});
  assert.throws(() => new EncryptedStore('another-key').read(file));
});

test('QR pairing, encrypted credentials, groups, logout and session isolation', async t => {
  const {manager, sockets, root} = setup(t);
  const id = randomUUID(), other = randomUUID();
  assert.equal(manager.status(id).state, 'disconnected');
  await manager.connect(id);
  sockets[0].ev.emit('connection.update', {qr: 'test-pairing-string'});
  for (let i=0; i<40 && !manager.qr(id); i++) await tick();
  assert.equal(manager.status(id).state, 'qr_required');
  assert.ok(Buffer.from(manager.qr(id).png_base64, 'base64').subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])));
  assert.equal(manager.qr(other), null);
  sockets[0].ev.emit('creds.update', {registered: true});
  sockets[0].ev.emit('connection.update', {connection: 'open'});
  assert.equal(manager.status(id).state, 'connected');
  assert.equal(manager.qr(id), null);
  assert.deepEqual(await manager.groups(id), [{external_chat_id: '123456789@g.us', name: 'My group'}]);
  assert.equal(manager.store.read(join(root, id, 'auth.enc')).creds.registered, true);
  assert.equal(readFileSync(join(root, id, 'auth.enc')).includes(Buffer.from('registered')), false);
  await manager.logout(id);
  assert.equal(manager.status(id).state, 'disconnected');
  assert.equal(manager.store.read(join(root,id,'auth.enc')).creds.registered, false);
  assert.throws(() => manager.session('../../escape'));
});

test('idempotency persists; server acknowledgements distinguish queued and sent', async t => {
  const {manager, sockets, sends, root} = setup(t);
  const id = randomUUID();
  await manager.connect(id); sockets[0].ev.emit('connection.update', {connection: 'open'});
  const first = manager.enqueue(id, 'caller-1', {recipient: '+201234567890', text: 'Hello'});
  assert.equal(manager.enqueue(id, 'caller-1', {recipient: '+201234567890', text: 'Hello'}).id, first.id);
  assert.throws(() => manager.enqueue(id, 'caller-1', {recipient: '+201234567890', text: 'Different'}));
  await tick();
  assert.equal(sends.length, 1);
  assert.equal(manager.message(id, first.id).status, 'processing');
  sockets[0].ev.emit('messages.update', [{key: {id: sends[0].options.messageId}, update: {status: 2}}]);
  assert.equal(manager.message(id, first.id).status, 'sent');
  const restored = new WhatsAppManager({root, storageKey: 'test-encryption-key'});
  assert.equal(restored.message(id, first.id).status, 'sent');
  assert.equal(restored.enqueue(id, 'caller-1', {recipient: '+201234567890', text: 'Hello'}).id, first.id);
  await restored.close();
});

test('restart does not resend processing messages; reconnect restores saved authentication', async t => {
  const {manager, sockets, root} = setup(t);
  const id = randomUUID();
  await manager.connect(id); sockets[0].ev.emit('creds.update', {registered: true});
  sockets[0].ev.emit('connection.update', {connection: 'open'});
  const operation = manager.enqueue(id, 'interrupted', {recipient: '123456789@g.us', text: 'Hello'});
  await tick();
  await manager.close();
  const restoredSockets = [];
  const restored = new WhatsAppManager({root, storageKey:'test-encryption-key', socketFactory: options => {
    const socket = {ev: new EventEmitter(), end() {}, options}; restoredSockets.push(socket); return socket;
  }});
  await restored.restore();
  assert.equal(restoredSockets.length, 1);
  assert.equal(restored.message(id,operation.id).status, 'unknown');
  assert.equal(restoredSockets[0].options.auth.creds.registered, true);
  await restored.close();
});

test('private HTTP API rejects missing auth, validates uploads, and scopes operation IDs', async t => {
  const {manager, sockets} = setup(t);
  const server = workerServer(manager, 'private-service-key');
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const base = 'http://127.0.0.1:' + server.address().port;
  assert.equal((await fetch(base+'/health')).status,401);
  const headers = {Authorization:'Bearer private-service-key'};
  assert.equal((await fetch(base+'/health',{headers})).status,200);
  const id=randomUUID(), other=randomUUID();
  const route=base+'/api/v1/sessions/'+id;
  assert.equal((await fetch(route+'/status',{headers})).status,200);
  let response=await fetch(route+'/messages',{method:'POST',headers:{...headers,'Content-Type':'application/json','Idempotency-Key':'text'},body:JSON.stringify({recipient:'123456789@g.us',text:'Hello'})});
  assert.equal(response.status,409);
  await manager.connect(id); sockets[0].ev.emit('connection.update',{connection:'open'});
  const fields=new FormData(); fields.set('recipient','123456789@g.us'); fields.set('caption','Picture');
  fields.set('file',new Blob([Buffer.from([137,80,78,71,13,10,26,10,1])],{type:'image/png'}),'picture.png');
  response=await fetch(route+'/messages',{method:'POST',headers:{...headers,'Idempotency-Key':'image'},body:fields});
  assert.equal(response.status,202); const operation=(await response.json()).data;
  assert.equal((await fetch(base+'/api/v1/sessions/'+other+'/messages/'+operation.id,{headers})).status,404);
  const bad=new FormData();bad.set('recipient','123456789@g.us');bad.set('file',new Blob(['invalid'],{type:'image/png'}),'bad.png');
  assert.equal((await fetch(route+'/messages',{method:'POST',headers:{...headers,'Idempotency-Key':'bad'},body:bad})).status,400);
  assert.equal(recipientJid('+201234567890'),'201234567890@s.whatsapp.net');
  assert.throws(()=>recipientJid('https://example.com'));
});
