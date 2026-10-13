(function() {
  const success = new URLSearchParams(location.search).get('success');
  let database;
  function db() {
    if (!database) database = new Promise((resolve, reject) => {
      const request = indexedDB.open('echosend-drafts', 1);
      request.onupgradeneeded = () => request.result.createObjectStore('drafts');
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    return database;
  }
  async function store(method, key, value) {
    const database = await db();
    return new Promise((resolve, reject) => {
      const transaction = database.transaction('drafts', method === 'get' ? 'readonly' : 'readwrite');
      const object = transaction.objectStore('drafts');
      const request = method === 'put' ? object.put(value, key) : object[method](key);
      transaction.oncomplete = () => resolve(request.result);
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error);
    });
  }
  document.addEventListener('DOMContentLoaded', async () => {
    const owner = document.body.dataset.userId;
    try {
      const pending = JSON.parse(sessionStorage.getItem('echosend-submitted-draft') || 'null');
      if (pending && pending.owner === owner && ['Job created','Job updated'].includes(success)) {
        await store('delete', pending.key);
        sessionStorage.removeItem('echosend-submitted-draft');
      }
    } catch { /* Draft failures never block the page. */ }
    const config = window.composerDraftConfig;
    if (!config || !config.owner) return;
    const form = document.getElementById('message-form');
    const root = document.getElementById('composer_draft');
    const status = root.querySelector('[role="status"]');
    const restore = root.querySelector('[data-action="restore"]');
    const discard = root.querySelector('[data-action="discard"]');
    const key = config.owner + ':' + location.pathname;
    let saved, timer, dirty = false, restoring = false, submitting = false, writes = Promise.resolve();
    function capture() {
      const fields = {};
      let attachment = null;
      for (const [name, value] of new FormData(form)) {
        if (['csrf_token','group_ids','group_names'].includes(name)) continue;
        if (name === 'image') { if (value.size) attachment = value; continue; }
        (fields[name] ||= []).push(value);
      }
      return {fields, attachment, recipients:config.selected(), updatedAt:Date.now()};
    }
    function save() {
      clearTimeout(timer);
      if (!dirty || restoring || submitting) return writes;
      const draft = capture(); dirty = false; restore.hidden = true;
      status.textContent = config.labels.saving;
      writes = writes.catch(() => {}).then(() => store('put', key, draft)).then(() => {
        status.textContent = config.labels.saved;
        discard.hidden = false;
      }).catch(() => {
        dirty = true; status.textContent = config.labels.unavailable;
      });
      return writes;
    }
    function changed() {
      if (restoring || submitting) return;
      dirty = true; clearTimeout(timer); timer = setTimeout(save, 400);
    }
    try {
      saved = await store('get', key);
      if (saved) {
        status.textContent = config.labels.available;
        restore.hidden = false; discard.hidden = false;
        // Failed submissions retain their validated fields and saved attachment.
        if (config.validationError && saved.attachment) {
          const files = new DataTransfer(); files.items.add(saved.attachment);
          form.querySelector('[name="image"]').files = files.files;
        }
      }
    } catch { status.textContent = config.labels.unavailable; }
    form.addEventListener('input', changed);
    form.addEventListener('change', () => { changed(); save(); });
    form.addEventListener('composer:change', changed);
    window.addEventListener('pagehide', save);
    restore.addEventListener('click', () => {
      if (!saved) return;
      restoring = true;
      for (const element of form.elements) {
        if (!element.name || ['csrf_token','image','group_ids','group_names'].includes(element.name)) continue;
        const values = saved.fields[element.name] || [];
        if (element.type === 'checkbox' || element.type === 'radio') element.checked = values.includes(element.value);
        else if (element.name in saved.fields) element.value = values[0] || '';
      }
      if (saved.attachment) {
        const files = new DataTransfer(); files.items.add(saved.attachment);
        form.querySelector('[name="image"]').files = files.files;
      }
      config.restore(saved.recipients);
      restoring = false;
      restore.hidden = true; status.textContent = config.labels.restored;
      dirty = true; save();
    });
    discard.addEventListener('click', async () => {
      clearTimeout(timer); dirty = false;
      try {
        await writes; await store('delete', key); saved = null;
        restore.hidden = true; discard.hidden = true; status.textContent = config.labels.discarded;
      } catch { status.textContent = config.labels.unavailable; }
    });
    form.addEventListener('submit', async event => {
      if (event.defaultPrevented) return;
      // Preserve the attachment if server-side validation rejects this form.
      event.preventDefault();
      if (submitting) return;
      dirty = true; const pendingSave = save(); submitting = true;
      form.querySelector('[type="submit"]').disabled = true;
      await pendingSave;
      try { sessionStorage.setItem('echosend-submitted-draft', JSON.stringify({owner:config.owner, key})); } catch {}
      HTMLFormElement.prototype.submit.call(form);
    });
  });
})();
