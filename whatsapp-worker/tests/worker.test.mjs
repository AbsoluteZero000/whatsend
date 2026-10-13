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
    const socket = {ev: new EventEmitter(), ws: new EventEmitter(), user: {id: '201234567890:1@s.whatsapp.net'},
      groupFetchAllParticipating: async () => ({group: {id: '123456789@g.us', subject: 'My group'}}),
      groupMetadata: async id => ({id, subject: 'My group'}),
      sendMessage: async (recipient, content, options) => { sends.push({recipient, content, options}); return {status: 1}; },
      logout: async () => {}, end: () => {}, config};
    sockets.push(socket); return socket;
  }});
  t.after(async () => { await manager.close(); rmSync(root, {recursive: true, force: true}); });
  return {manager, sockets, sends, root};
}

test('picker exposes announcement chats instead of community containers', async t => {
  const {manager, sockets} = setup(t);
  const id = randomUUID();
  await manager.connect(id); sockets[0].ev.emit('connection.update', {connection: 'open'});
  sockets[0].groupFetchAllParticipating = async () => ({
    '100@g.us': {id: '100@g.us', subject: 'Our community', isCommunity: true},
    '101@g.us': {id: '101@g.us', subject: 'Announcements', isCommunityAnnounce: true, linkedParent: '100@g.us'},
    '102@g.us': {id: '102@g.us', subject: 'Discussion', linkedParent: '100@g.us'},
    '103@g.us': {id: '103@g.us', subject: 'Other community', isCommunityAnnounce: true, linkedParent: '999@g.us'},
  });
  assert.deepEqual(await manager.groups(id), [
    {external_chat_id: '101@g.us', name: 'Our community — Announcements'},
    {external_chat_id: '102@g.us', name: 'Our community — Discussion'},
    {external_chat_id: '103@g.us', name: 'Other community — Announcements'},
  ]);
});

test('community text and media sends resolve the linked announcement group; direct targets stay intact', async t => {
  const {manager, sockets, sends} = setup(t);
  const id = randomUUID();
  await manager.connect(id); sockets[0].ev.emit('connection.update', {connection: 'open'});
  sockets[0].groupMetadata = async jid => ({id: jid, isCommunity: jid === '100@g.us', isCommunityAnnounce: jid === '101@g.us'});
  sockets[0].groupFetchAllParticipating = async () => ({
    unrelated: {id: '201@g.us', isCommunityAnnounce: true, linkedParent: '200@g.us'},
    announcement: {id: '101@g.us', isCommunityAnnounce: true, linkedParent: '100@g.us'},
  });
  const text = manager.enqueue(id, 'community-text', {recipient: '100@g.us', text: 'News'});
  const bytes = Buffer.from('%PDF-1.7 document');
  manager.enqueue(id, 'community-media', {recipient: '100@g.us', caption: 'Report',
    media: {kind: 'document', bytes, mime: 'application/pdf', filename: 'Report.pdf'}});
  manager.enqueue(id, 'direct-announcement', {recipient: '101@g.us', text: 'Direct announcement'});
  manager.enqueue(id, 'discussion', {recipient: '102@g.us', text: 'Discussion'});
  manager.enqueue(id, 'personal', {recipient: '+201234567890', text: 'Personal'});
  await tick();
  assert.deepEqual(sends.map(send => send.recipient), ['101@g.us', '101@g.us', '101@g.us', '102@g.us', '201234567890@s.whatsapp.net']);
  assert.deepEqual(sends[1].content.document, bytes);
  assert.equal(sends[1].content.caption, 'Report');
  assert.equal(sends[1].content.fileName, 'Report.pdf');
  assert.equal(manager.enqueue(id, 'community-text', {recipient: '100@g.us', text: 'News'}).id, text.id);
  await tick();
  assert.equal(sends.length, 5);
  sockets[0].ev.emit('messages.update', [{key: {id: sends[0].options.messageId}, update: {status: 2}}]);
  assert.equal(manager.message(id, text.id).status, 'sent');
});

test('missing or ambiguous announcement groups fail before sending and do not block later messages', async t => {
  const {manager, sockets, sends} = setup(t);
  const id = randomUUID();
  await manager.connect(id); sockets[0].ev.emit('connection.update', {connection: 'open'});
  sockets[0].groupMetadata = async jid => ({id: jid, isCommunity: true});
  for (const groups of [{}, {
    first: {id: '101@g.us', isCommunityAnnounce: true, linkedParent: '100@g.us'},
    second: {id: '102@g.us', isCommunityAnnounce: true, linkedParent: '100@g.us'},
  }]) {
    sockets[0].groupFetchAllParticipating = async () => groups;
    const operation = manager.enqueue(id, randomUUID(), {recipient: '100@g.us', text: 'News'});
    await tick();
    assert.equal(manager.message(id, operation.id).status, 'failed');
    assert.match(manager.message(id, operation.id).error, /announcement group is unavailable/);
    assert.equal(sends.length, 0);
  }
  manager.enqueue(id, 'next-message', {recipient: '+201234567890', text: 'Next'});
  await tick();
  assert.equal(sends.length, 1);
});

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


test('direct server ACK confirms text and media without recipient receipts, including late ACKs', async t => {
  const {manager, sockets, sends, root} = setup(t);
  const id = randomUUID();
  await manager.connect(id); sockets[0].ev.emit('connection.update', {connection: 'open'});
  for (const [key, payload] of [
    ['ack-text', {recipient: '+201234567890', text: 'Hello'}],
    ['ack-media', {recipient: '123456789@g.us', caption: 'Report', media: {
      kind: 'document', bytes: Buffer.from('%PDF-1.7'), mime: 'application/pdf', filename: 'report.pdf'}}],
  ]) {
    const operation = manager.enqueue(id, key, payload); await tick();
    assert.equal(manager.message(id, operation.id).status, 'processing');
    manager.session(id).journal[operation.id].attemptedAt = Date.now() - 120001;
    assert.equal(manager.message(id, operation.id).status, 'unknown');
    sockets[0].ws.emit('CB:ack,class:message', {attrs: {id: sends.at(-1).options.messageId, class: 'message'}});
    assert.deepEqual(manager.message(id, operation.id), {id: operation.id, status: 'sent', error: null});
  }
  assert.equal(sends.length, 2);
  const journal = manager.store.read(join(root, id, 'messages.enc'));
  assert.ok(Object.values(journal).every(o => o.status === 'sent'));
});

test('error ACKs fail and obsolete socket ACKs cannot change a message', async t => {
  const {manager, sockets, sends} = setup(t);
  const id = randomUUID(); await manager.connect(id);
  sockets[0].ev.emit('connection.update', {connection: 'open'});
  const operation = manager.enqueue(id, 'ack-error', {recipient: '+201234567890', text: 'Hello'});
  await tick();
  const waId = sends[0].options.messageId;
  sockets[0].ws.emit('CB:ack,class:message', {attrs: {id: waId, error: '403'}});
  assert.equal(manager.message(id, operation.id).status, 'failed');
  await manager.logout(id);
  sockets[0].ws.emit('CB:ack,class:message', {attrs: {id: waId}});
  assert.equal(manager.message(id, operation.id).status, 'failed');
});

test('separate community list exposes announcements and names subgroups without duplicates', async t => {
  const {manager, sockets} = setup(t);
  const id = randomUUID(); await manager.connect(id);
  sockets[0].ev.emit('connection.update', {connection: 'open'});
  sockets[0].groupFetchAllParticipating = async () => ({
    discussion: {id: '102@g.us', subject: 'Discussion', linkedParent: '100@g.us'},
    announcement: {id: '101@g.us', subject: 'Announcements', isCommunityAnnounce: true, linkedParent: '100@g.us'},
  });
  sockets[0].communityFetchAllParticipating = async () => ({
    parent: {id: '100@g.us', subject: 'Our community', isCommunity: true},
    announcement: {id: '101@g.us', subject: 'Announcements', isCommunityAnnounce: false, linkedParent: '100@g.us'},
  });
  assert.deepEqual(await manager.groups(id), [
    {external_chat_id: '102@g.us', name: 'Our community — Discussion'},
    {external_chat_id: '101@g.us', name: 'Our community — Announcements'},
  ]);
});

test('missing announcement is discovered from linked metadata and community IDs resolve for text/media', async t => {
  const {manager, sockets, sends} = setup(t);
  const id = randomUUID(); await manager.connect(id);
  sockets[0].ev.emit('connection.update', {connection: 'open'});
  sockets[0].groupFetchAllParticipating = async () => ({});
  sockets[0].communityFetchAllParticipating = async () => ({
    parent: {id: '100@g.us', subject: 'Our community', isCommunity: true},
  });
  sockets[0].communityFetchLinkedGroups = async jid => {
    assert.equal(jid, '100@g.us');
    return {linkedGroups: [{id: '102@g.us'}, {id: '101@g.us'}, {id: '103@g.us'}]};
  };
  sockets[0].groupMetadata = async jid => {
    if (jid === '100@g.us' || jid === '103@g.us') throw new Error('Unavailable group');
    return {id: jid, subject: jid === '101@g.us' ? 'Updates' : 'Announcements',
      isCommunityAnnounce: jid === '101@g.us', linkedParent: '100@g.us'};
  };
  sockets[0].communityMetadata = async jid => ({id: jid, subject: 'Our community', isCommunity: true});
  assert.deepEqual(await manager.groups(id), [
    {external_chat_id: '101@g.us', name: 'Our community — Announcements'},
  ]);
  manager.enqueue(id, 'linked-text', {recipient: '100@g.us', text: 'News'});
  manager.enqueue(id, 'linked-media', {recipient: '100@g.us', caption: 'Report', media: {
    kind: 'document', bytes: Buffer.from('%PDF-1.7'), mime: 'application/pdf', filename: 'report.pdf'}});
  await tick();
  assert.deepEqual(sends.map(send => send.recipient), ['101@g.us', '101@g.us']);
  assert.equal(sends[1].content.fileName, 'report.pdf');
});

test('community lookup outages retain ordinary groups and fail unresolved sends before transmission', async t => {
  const {manager, sockets, sends} = setup(t);
  const id = randomUUID(); await manager.connect(id);
  sockets[0].ev.emit('connection.update', {connection: 'open'});
  sockets[0].communityFetchAllParticipating = async () => { throw new Error('Unavailable'); };
  sockets[0].communityFetchLinkedGroups = async () => { throw new Error('Unavailable'); };
  assert.deepEqual(await manager.groups(id), [{external_chat_id: '123456789@g.us', name: 'My group'}]);
  sockets[0].groupMetadata = async jid => ({id: jid, isCommunity: true});
  const operation = manager.enqueue(id, 'community-unavailable', {recipient: '100@g.us', text: 'News'});
  await tick();
  assert.equal(manager.message(id, operation.id).status, 'failed');
  assert.match(manager.message(id, operation.id).error, /announcement group is unavailable/);
  assert.equal(sends.length, 0);
});

test('recipients queued during an in-flight send are drained exactly once', async t => {
  const {manager, sockets, sends} = setup(t);
  const id = randomUUID(); await manager.connect(id);
  sockets[0].ev.emit('connection.update', {connection: 'open'});
  let releaseFirst, startFirst;
  const blocked = new Promise(resolve => { releaseFirst = resolve; });
  const started = new Promise(resolve => { startFirst = resolve; });
  sockets[0].sendMessage = async (recipient, content, options) => {
    sends.push({recipient, content, options});
    if (sends.length === 1) { startFirst(); await blocked; }
    return {status: 2};
  };
  const first = manager.enqueue(id, 'batch-first', {recipient: '+201234567890', text: 'Hello'});
  await started;
  const second = manager.enqueue(id, 'batch-second', {recipient: '123456789@g.us', text: 'Hello'});
  const third = manager.enqueue(id, 'batch-third', {recipient: '+201234567891', caption: 'Report', media: {
    kind: 'document', bytes: Buffer.from('%PDF-1.7'), mime: 'application/pdf', filename: 'report.pdf'}});
  await tick();
  assert.equal(sends.length, 1);
  releaseFirst(); await tick();
  assert.deepEqual(sends.map(send => send.recipient), ['201234567890@s.whatsapp.net', '123456789@g.us', '201234567891@s.whatsapp.net']);
  for (const operation of [first, second, third]) assert.equal(manager.message(id, operation.id).status, 'sent');
  await manager.drain(manager.session(id));
  assert.equal(sends.length, 3);
});
