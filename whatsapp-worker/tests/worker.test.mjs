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
import {mediaKind} from '../media.mjs';

const tick = () => new Promise(resolve => setTimeout(resolve, 25));

test('all supported document formats validate; spoofed documents and oversize bodies fail', () => {
  const zip = Buffer.from([80,75,3,4]), ole = Buffer.from([208,207,17,224,161,177,26,225]);
  const cases = [
    ['pdf','application/pdf',Buffer.from('%PDF-1.7')], ['doc','application/msword',ole],
    ['docx','application/vnd.openxmlformats-officedocument.wordprocessingml.document',zip],
    ['xls','application/vnd.ms-excel',ole], ['xlsx','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',zip],
    ['ppt','application/vnd.ms-powerpoint',ole], ['pptx','application/vnd.openxmlformats-officedocument.presentationml.presentation',zip],
    ['odt','application/vnd.oasis.opendocument.text',zip], ['ods','application/vnd.oasis.opendocument.spreadsheet',zip],
    ['odp','application/vnd.oasis.opendocument.presentation',zip], ['zip','application/zip',zip],
    ['txt','text/plain',Buffer.from('hello')], ['csv','text/csv',Buffer.from('a,b')], ['rtf','application/rtf',Buffer.from('{\\rtf1 text}')],
  ];
  for (const [ext,mime,bytes] of cases) assert.equal(mediaKind({filename:'Report.'+ext,mime,bytes}),'document',ext);
  assert.equal(mediaKind({filename:'bad.pdf',mime:'application/pdf',bytes:Buffer.from('MZexecutable')}),null);
  assert.equal(mediaKind({filename:'bad.docx',mime:'application/msword',bytes:ole}),null);
  assert.equal(mediaKind({filename:'empty.txt',mime:'text/plain',bytes:Buffer.alloc(0)}),null);
  const big = Buffer.alloc(50*1024*1024+1, 65);
  assert.equal(mediaKind({filename:'huge.txt',mime:'text/plain',bytes:big}),null);
});

test('document uploads reach Baileys with original filename, caption and bytes', async t => {
  const {manager,sockets,sends} = setup(t);
  const server = workerServer(manager,'document-secret');
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const id=randomUUID(); await manager.connect(id);
  sockets[0].ev.emit('connection.update',{connection:'open'});
  const bytes=Buffer.from('%PDF-1.7 document');
  const form=new FormData(); form.set('recipient','123456789@g.us'); form.set('caption','Quarterly report');
  form.set('file',new Blob([bytes],{type:'application/pdf'}),'Report.pdf');
  const response=await fetch('http://127.0.0.1:'+server.address().port+'/api/v1/sessions/'+id+'/messages',{
    method:'POST',headers:{Authorization:'Bearer document-secret','Idempotency-Key':'document-1'},body:form});
  assert.equal(response.status,202); await tick();
  assert.equal(sends.length,1);
  assert.deepEqual(sends[0].content.document,bytes);
  assert.equal(sends[0].content.fileName,'Report.pdf');
  assert.equal(sends[0].content.mimetype,'application/pdf');
  assert.equal(sends[0].content.caption,'Quarterly report');
  assert.equal(sends[0].content.image,undefined);
});
test('document uploads keep Arabic filenames intact', async t => {
  const {manager,sockets,sends} = setup(t);
  const server = workerServer(manager,'document-secret');
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const id=randomUUID(); await manager.connect(id);
  sockets[0].ev.emit('connection.update',{connection:'open'});
  const form=new FormData(); form.set('recipient','123456789@g.us');
  form.set('file',new Blob([Buffer.from('%PDF-1.7 document')],{type:'application/pdf'}),'تقرير الربع الأول.pdf');
  const response=await fetch('http://127.0.0.1:'+server.address().port+'/api/v1/sessions/'+id+'/messages',{
    method:'POST',headers:{Authorization:'Bearer document-secret','Idempotency-Key':'document-ar-1'},body:form});
  assert.equal(response.status,202); await tick();
  assert.equal(sends.length,1);
  assert.equal(sends[0].content.fileName,'تقرير الربع الأول.pdf');
});
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
  await manager.connect(id); sockets[0].ev.emit('creds.update', {me: {id: '201234567890:1@s.whatsapp.net'}});
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
  assert.equal(restoredSockets[0].options.auth.creds.registered, false);
  assert.equal(restoredSockets[0].options.auth.creds.me.id, '201234567890:1@s.whatsapp.net');
  assert.equal(restored.status(id).state, 'reconnecting');
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
