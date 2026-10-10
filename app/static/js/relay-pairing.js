(function () {
  const script = document.querySelector('script[data-token-id][data-messages]');
  const messages = JSON.parse(script.dataset.messages);
  const root = '/tokens/' + script.dataset.tokenId + '/relay';
  const status = document.getElementById('pair_status');
  const error = document.getElementById('pair_error');
  const qr = document.getElementById('pair_qr');
  const image = document.getElementById('pair_qr_image');
  const connect = document.getElementById('pair_connect');
  const refresh = document.getElementById('pair_refresh');
  let pollTimer, expiryTimer, controller;
  let closed = false;
  let busy = false;
  let actionError = '';

  function clearQr() {
    clearTimeout(expiryTimer);
    qr.hidden = true;
    image.removeAttribute('src');
  }

  function display(data) {
    if (closed) return true;
    if (data.state === 'connected') actionError = '';
    error.textContent = actionError;
    error.hidden = !actionError;
    status.textContent = messages[data.state] || messages.checking;
    connect.hidden = data.state === 'connected';
    clearQr();
    if (data.qr) {
      const expiry = new Date(data.qr.expires_at);
      const remaining = expiry.getTime() - Date.now();
      if (remaining > 0) {
        image.src = 'data:image/png;base64,' + data.qr.png_base64;
        qr.hidden = false;
        document.getElementById('pair_expiry').textContent = messages.expiry + ' ' + expiry.toLocaleTimeString();
        expiryTimer = setTimeout(function () {
          clearQr();
          status.textContent = messages.expired;
          poll();
        }, Math.min(remaining, 2147483647));
      } else {
        status.textContent = messages.expired;
      }
    }
    return data.state === 'connected';
  }

  async function request(path, method) {
    controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 35000);
    try {
      const response = await fetch(root + path, {
        method: method || 'GET', credentials: 'same-origin', cache: 'no-store',
        signal: controller.signal,
        headers: {'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content}
      });
      if (response.redirected) {
        closed = true;
        window.location.assign(response.url);
        throw new Error(messages.request_failed);
      }
      const data = await response.json();
      if (!response.ok) {
        const detail = data.detail;
        throw new Error(detail === 'Relay key needs sessions:read and sessions:write permissions for pairing.'
          ? messages.permissions : detail || messages.request_failed);
      }
      return data;
    } finally {
      clearTimeout(timeout);
      controller = null;
    }
  }

  async function poll() {
    if (closed || busy) return;
    clearTimeout(pollTimer);
    busy = true;
    refresh.disabled = true;
    let connected = false;
    try {
      connected = display(await request('/state'));
    } catch (exc) {
      clearQr();
      status.textContent = messages.error;
      error.textContent = exc.name === 'AbortError' ? messages.request_failed : exc.message;
      error.hidden = false;
    } finally {
      busy = false;
      refresh.disabled = false;
      if (!closed && !connected) pollTimer = setTimeout(poll, 5000);
    }
  }

  connect.addEventListener('click', async function () {
    if (closed || busy) return;
    clearTimeout(pollTimer);
    clearQr();
    busy = true;
    connect.disabled = true;
    refresh.disabled = true;
    status.textContent = messages.connecting;
    actionError = '';
    error.hidden = true;
    try {
      display(await request('/connect', 'POST'));
    } catch (exc) {
      actionError = exc.name === 'AbortError' ? messages.request_failed : exc.message;
      error.textContent = actionError;
      error.hidden = false;
    } finally {
      busy = false;
      connect.disabled = false;
      refresh.disabled = false;
      if (!closed) poll();
    }
  });
  refresh.addEventListener('click', poll);
  window.addEventListener('pagehide', function () {
    closed = true;
    clearTimeout(pollTimer);
    clearQr();
    if (controller) controller.abort();
  });
  window.addEventListener('pageshow', function (event) {
    if (event.persisted) {
      closed = false;
      poll();
    }
  });
  poll();
})();
