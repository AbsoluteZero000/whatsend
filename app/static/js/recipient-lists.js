window.createRecipientLists = function(config) {
  const root = document.getElementById('recipient_lists');
  const picker = root.querySelector('select');
  const name = root.querySelector('input');
  const status = root.querySelector('[role="status"]');
  const add = root.querySelector('[data-action="add"]');
  const remove = root.querySelector('[data-action="delete"]');
  const save = root.querySelector('[data-action="save"]');
  let lists = [], generation = 0, busy = false;
  const headers = () => ({'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content});
  function controls() {
    add.disabled = busy || !picker.value;
    remove.disabled = busy || !picker.value;
    save.disabled = busy || !config.connection.value;
  }
  function render(selected) {
    picker.replaceChildren(new Option(config.labels.choose, ''));
    lists.forEach(list => picker.add(new Option(list.name + ' (' + list.recipients.length + ')', list.id)));
    picker.value = selected || ''; controls();
  }
  async function load() {
    const request = ++generation;
    lists = []; render(); status.textContent = '';
    if (!config.connection.value) return;
    try {
      const response = await fetch('/jobs/recipient-lists?token_id=' + encodeURIComponent(config.connection.value), {cache:'no-store'});
      if (!response.ok || response.redirected) throw new Error();
      const data = await response.json();
      if (request !== generation) return;
      lists = data.lists; render();
    } catch { if (request === generation) status.textContent = config.labels.loadError; }
  }
  picker.addEventListener('change', controls);
  add.addEventListener('click', () => {
    const list = lists.find(list => String(list.id) === picker.value);
    if (!list) return;
    config.add(list.recipients);
    status.textContent = config.labels.added;
  });
  save.addEventListener('click', async () => {
    const recipients = config.selected();
    if (!name.value.trim() || !recipients.length) { status.textContent = config.labels.required; name.focus(); return; }
    const token = config.connection.value, request = generation;
    busy = true; controls(); status.textContent = config.labels.saving;
    try {
      const response = await fetch('/jobs/recipient-lists', {method:'POST', headers:headers(),
        body:JSON.stringify({token_id:Number(token), name:name.value.trim(), recipients})});
      if (!response.ok || response.redirected) throw new Error();
      const list = await response.json();
      if (request !== generation) return;
      lists.push(list); render(String(list.id)); name.value = ''; status.textContent = config.labels.saved;
    } catch { if (request === generation) status.textContent = config.labels.saveError; }
    finally { busy = false; controls(); }
  });
  remove.addEventListener('click', async () => {
    const list = lists.find(list => String(list.id) === picker.value);
    if (!list || !window.confirm(config.labels.confirmDelete + ' ' + list.name + '?')) return;
    const request = generation;
    busy = true; controls();
    try {
      const response = await fetch('/jobs/recipient-lists/' + list.id, {method:'DELETE', headers:headers()});
      if (!response.ok || response.redirected) throw new Error();
      if (request !== generation) return;
      lists = lists.filter(item => item.id !== list.id); render(); status.textContent = config.labels.deleted;
    } catch { if (request === generation) status.textContent = config.labels.saveError; }
    finally { busy = false; controls(); }
  });
  config.connection.addEventListener('change', load);
  load();
  return {load};
};
