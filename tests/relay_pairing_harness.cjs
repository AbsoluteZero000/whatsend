const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// Execute the actual script with a small DOM/clock surface and mocked requests.
// No browser, real credentials, Relay instance, or WhatsApp session is involved.
function setup() {
  const elements = {};
  const timers = new Map();
  const listeners = {};
  const calls = [];
  let counter = 0;
  let state = 'disconnected';
  let denyConnect = false;
  let clock = Date.now();
  let expired = false;
  for (const id of ['pair_status', 'pair_error', 'pair_qr', 'pair_qr_image', 'pair_connect', 'pair_refresh', 'pair_expiry']) {
    elements[id] = {hidden: false, disabled: false, events: {}, addEventListener(name, fn) { this.events[name] = fn; }, removeAttribute(name) { delete this[name]; }};
  }
  const messages = {
    checking: 'Checking', disconnected: 'Start pairing', connecting: 'Preparing',
    qr_required: 'Scan', connected: 'Connected', reconnecting: 'Reconnecting',
    failed: 'Failed', expired: 'Expired', expiry: 'Valid until', error: 'Check failed',
    request_failed: 'Request failed', permissions: 'Pairing permissions required',
  };
  const script = {dataset: {tokenId: '3', messages: JSON.stringify(messages)}};
  const window = {location: {assign(url) { this.url = url; }}, addEventListener(name, fn) { listeners[name] = fn; }};
  class ClockDate extends Date { static now() { return clock; } }
  vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), {
    document: {
      querySelector(selector) { return selector.startsWith('script') ? script : {content: 'csrf-test'}; },
      getElementById(id) { return elements[id]; },
    }, window, Date: ClockDate, AbortController,
    setTimeout(fn, delay) { const id = ++counter; timers.set(id, {fn, delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
    async fetch(url, options) {
      calls.push({url, options});
      if (url.endsWith('/connect')) {
        if (denyConnect) return {ok: false, json: async () => ({detail: 'Relay key needs sessions:read and sessions:write permissions for pairing.'})};
        state = 'qr_required';
        return {ok: true, json: async () => ({state})};
      }
      return {ok: true, json: async () => ({state, qr: state === 'qr_required' ? {
        png_base64: 'test-png', expires_at: new Date(clock + (expired ? -1000 : 60000)).toISOString(),
      } : null})};
    },
  });
  return {elements, timers, listeners, calls, setState(value) { state = value; }, deny() { denyConnect = true; }, expire() { expired = true; clock += 61000; }};
}

const settle = () => new Promise(resolve => setImmediate(resolve));

(async () => {
  const ui = setup();
  await settle();
  assert.equal(ui.elements.pair_status.textContent, 'Start pairing');
  assert(ui.calls.every(call => call.options.method === 'GET'));
  await ui.elements.pair_connect.events.click();
  await settle();
  assert.equal(ui.elements.pair_qr.hidden, false);
  assert.equal(ui.elements.pair_qr_image.src, 'data:image/png;base64,test-png');
  assert.equal(ui.calls.filter(call => call.options.method === 'POST').length, 1);
  assert.equal(ui.calls.find(call => call.options.method === 'POST').options.headers['X-CSRF-Token'], 'csrf-test');

  ui.expire();
  const expiryTimer = [...ui.timers.values()].find(timer => timer.delay === 60000);
  assert(expiryTimer);
  expiryTimer.fn();
  await settle();
  assert.equal(ui.elements.pair_qr.hidden, true);
  assert.equal(ui.elements.pair_qr_image.src, undefined);
  assert.equal(ui.elements.pair_status.textContent, 'Expired');

  ui.setState('connected');
  await ui.elements.pair_refresh.events.click();
  assert.equal(ui.elements.pair_status.textContent, 'Connected');
  assert.equal(ui.elements.pair_connect.hidden, true);
  assert.equal(ui.elements.pair_qr.hidden, true);
  assert.equal(ui.timers.size, 0);

  const denied = setup();
  await settle();
  denied.deny();
  await denied.elements.pair_connect.events.click();
  await settle();
  assert.equal(denied.elements.pair_error.hidden, false);
  assert.equal(denied.elements.pair_error.textContent, 'Pairing permissions required');
  denied.listeners.pagehide();
  assert.equal(denied.timers.size, 0);
  assert.equal(denied.elements.pair_qr_image.src, undefined);
  denied.listeners.pageshow({persisted: true});
  await settle();
  assert([...denied.timers.values()].some(timer => timer.delay === 5000));
  console.log('QR refresh, expiry, connection, permissions, and navigation checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
