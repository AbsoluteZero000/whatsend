// Changing text: a list whose next item replaces {word} in the message at each send.
function createWordList(config) {
  const TOKEN = '{word}';
  const labels = config.labels;
  const message = document.getElementById('message');
  const add = document.getElementById('word_list_add');
  const panel = document.getElementById('word_list_panel');
  const list = document.getElementById('word_list');
  const next = document.getElementById('word_list_next');
  const missing = panel.querySelector('.word-list-missing');
  const count = panel.querySelector('.word-list-count');
  const numbers = document.getElementById('word_list_numbers');
  const numbersToggle = panel.querySelector('[data-action="numbers"]');
  const previewList = panel.querySelector('.word-list-preview ol');
  const after = panel.querySelector('[data-role="after"]');
  const mode = document.getElementById('message_mode');
  const trigger = document.getElementById('trigger_type');
  let caret = null;

  const items = () => list.value.split('\n').map(line => line.trim()).filter(Boolean);
  const endMode = () => (panel.querySelector('[name="word_list_end"]:checked') || {}).value || 'repeat';
  const changed = () => document.getElementById('message-form').dispatchEvent(new Event('composer:change'));

  function rememberCaret() { caret = message.selectionStart; }
  ['keyup', 'click', 'select', 'blur'].forEach(name => message.addEventListener(name, rememberCaret));

  function insertToken() {
    const value = message.value;
    const at = caret === null || caret > value.length ? value.length : caret;
    const before = at && !/\s/.test(value[at - 1]) ? ' ' : '';
    const afterSpace = at < value.length && !/[\s.,!?؟،]/.test(value[at]) ? ' ' : '';
    message.value = value.slice(0, at) + before + TOKEN + afterSpace + value.slice(at);
    caret = at + before.length + TOKEN.length;
    message.dispatchEvent(new Event('input', {bubbles: true}));
    message.focus();
    message.setSelectionRange(caret - TOKEN.length, caret);
  }

  function fillNumbers() {
    const field = name => parseInt(numbers.querySelector('[data-field="' + name + '"]').value, 10);
    const from = field('from'), to = field('to'), size = Math.max(1, field('size') || 1);
    if (isNaN(from) || isNaN(to)) return;
    const step = from <= to ? 1 : -1, lines = [];
    for (let start = from; step > 0 ? start <= to : start >= to; start += step * size) {
      const end = step > 0 ? Math.min(start + size - 1, to) : Math.max(start - size + 1, to);
      lines.push(start === end ? String(start) : start + '-' + end);
      if (lines.length >= config.maxItems) break;
    }
    list.value = lines.join('\n');
    list.dispatchEvent(new Event('input', {bubbles: true}));
  }

  function previewItem(text, item) {
    const row = document.createElement('li'), body = document.createElement('span');
    text.split(TOKEN).forEach((part, index) => {
      if (index) { const mark = document.createElement('mark'); mark.textContent = item; body.appendChild(mark); }
      body.appendChild(document.createTextNode(part));
    });
    row.appendChild(body);
    return row;
  }

  function refresh() {
    const series = mode && mode.value === 'series';
    const words = items();
    const open = !panel.hidden;
    add.hidden = series || open;
    panel.classList.toggle('is-series', series);
    if (series) { panel.hidden = true; return; }
    if (!open) return;
    const text = message.value;
    missing.hidden = text.includes(TOKEN) || !words.length;
    count.textContent = words.length ? labels.count.replace('{count}', words.length) : labels.empty;
    if (next) {
      const chosen = parseInt(next.value || next.dataset.initial || '0', 10) || 0;
      next.replaceChildren(...words.map((word, index) => new Option((index + 1) + '. ' + word, String(index))));
      next.value = String(Math.min(chosen, Math.max(words.length - 1, 0)));
    }
    const start = next ? parseInt(next.value || '0', 10) || 0 : 0;
    const shown = [];
    for (let send = 0; send < Math.min(3, words.length); send++) {
      const index = endMode() === 'stop' ? start + send : (start + send) % words.length;
      if (index < words.length) shown.push(words[index]);
    }
    const base = text.includes(TOKEN) ? text : (text ? text + ' ' : '') + TOKEN;
    previewList.replaceChildren(...shown.map(word => previewItem(base, word)));
    previewList.hidden = !shown.length;
    const oneTime = trigger && ['now', 'date'].includes(trigger.value);
    if (!words.length) after.textContent = labels.previewEmpty;
    else if (oneTime) after.textContent = labels.oneTime;
    else if (endMode() === 'stop') after.textContent = labels.stops.replace('{count}', words.length - start);
    else after.textContent = labels.repeats.replace('{first}', words[0]);
  }

  add.addEventListener('click', () => {
    panel.hidden = false;
    if (!message.value.includes(TOKEN)) insertToken();
    refresh(); changed();
    list.focus();
  });
  panel.querySelector('[data-action="remove"]').addEventListener('click', () => {
    list.value = '';
    message.value = message.value.replace(/ ?\{word\}/g, '');
    message.dispatchEvent(new Event('input', {bubbles: true}));
    panel.hidden = true; numbers.hidden = true; numbersToggle.setAttribute('aria-expanded', 'false');
    refresh(); changed();
    add.focus();
  });
  panel.querySelector('[data-action="insert"]').addEventListener('click', () => { insertToken(); refresh(); });
  numbersToggle.addEventListener('click', () => {
    numbers.hidden = !numbers.hidden;
    numbersToggle.setAttribute('aria-expanded', String(!numbers.hidden));
  });
  panel.querySelector('[data-action="fill"]').addEventListener('click', fillNumbers);
  [message, list].forEach(element => element.addEventListener('input', refresh));
  panel.querySelectorAll('[name="word_list_end"]').forEach(radio => radio.addEventListener('change', refresh));
  if (next) next.addEventListener('change', refresh);
  if (mode) mode.addEventListener('change', refresh);
  if (trigger) trigger.addEventListener('change', refresh);

  // A message that already uses {word} (edit, failed submit, draft) opens with its list.
  if (items().length || message.value.includes(TOKEN)) panel.hidden = false;
  refresh();

  return {
    refresh() { if (items().length || message.value.includes(TOKEN)) panel.hidden = false; refresh(); },
    validate() {
      if (panel.hidden || (mode && mode.value === 'series')) return true;
      const words = items();
      if (words.length && !message.value.includes(TOKEN)) { missing.hidden = false; missing.scrollIntoView({behavior: 'smooth', block: 'center'}); return false; }
      if (!words.length && message.value.includes(TOKEN)) { count.textContent = labels.empty; list.focus(); return false; }
      return true;
    }
  };
}
