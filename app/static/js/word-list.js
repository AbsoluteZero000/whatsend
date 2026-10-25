// Changing text: lists whose next items replace {word}, {word2}… in the message at each send.
function createWordList(config) {
  const TOKENS = /\{word(\d*)\}/g;
  const labels = config.labels;
  const message = document.getElementById('message');
  const add = document.getElementById('word_list_add');
  const panel = document.getElementById('word_list_panel');
  const store = document.getElementById('word_lists');
  const partsBox = panel.querySelector('.word-list-parts');
  const template = document.getElementById('word_part_template');
  const addPart = panel.querySelector('[data-action="add-part"]');
  const removeAll = panel.querySelector('[data-action="remove"]');
  const together = panel.querySelector('[data-role="together"]');
  const next = document.getElementById('word_list_next');
  const previewList = panel.querySelector('.word-list-preview ol');
  const after = panel.querySelector('[data-role="after"]');
  const mode = document.getElementById('message_mode');
  const trigger = document.getElementById('trigger_type');
  let caret = null;

  const token = number => number === 1 ? '{word}' : '{word' + number + '}';
  const fillIn = (text, values) => text.replace(/\{(\w+)\}/g, (match, key) => key in values ? values[key] : match);
  const parts = () => Array.from(partsBox.children);
  const itemsOf = part => part.querySelector('textarea').value.split('\n').map(line => line.trim()).filter(Boolean);
  const lists = () => parts().map(itemsOf);
  const longest = () => Math.max(0, ...lists().map(items => items.length));
  const used = () => new Set(Array.from(message.value.matchAll(TOKENS), match => parseInt(match[1] || '1', 10)));
  const endMode = () => (panel.querySelector('[name="word_list_end"]:checked') || {}).value || 'repeat';
  const changed = () => document.getElementById('message-form').dispatchEvent(new Event('composer:change'));

  function rememberCaret() { caret = message.selectionStart; }
  ['keyup', 'click', 'select', 'blur'].forEach(name => message.addEventListener(name, rememberCaret));

  function insertToken(text) {
    const value = message.value;
    const at = caret === null || caret > value.length ? value.length : caret;
    const before = at && !/\s/.test(value[at - 1]) ? ' ' : '';
    const afterSpace = at < value.length && !/[\s.,!?؟،]/.test(value[at]) ? ' ' : '';
    message.value = value.slice(0, at) + before + text + afterSpace + value.slice(at);
    caret = at + before.length + text.length;
    message.dispatchEvent(new Event('input', {bubbles: true}));
    message.focus();
    message.setSelectionRange(caret - text.length, caret);
  }

  // Take a part's placeholder out of the message and move later parts' placeholders down by one.
  function dropToken(number) {
    message.value = message.value
      .replace(new RegExp(' ?\\{word' + (number === 1 ? '1?' : number) + '\\}', 'g'), '')
      .replace(TOKENS, (match, digits) => {
        const current = parseInt(digits || '1', 10);
        return current > number ? token(current - 1) : match;
      });
    message.dispatchEvent(new Event('input', {bubbles: true}));
  }

  function fillNumbers(part) {
    const box = part.querySelector('.word-list-numbers');
    const field = name => parseInt(box.querySelector('[data-field="' + name + '"]').value, 10);
    const from = field('from'), to = field('to'), size = Math.max(1, field('size') || 1);
    if (isNaN(from) || isNaN(to)) return;
    const step = from <= to ? 1 : -1, lines = [];
    for (let start = from; step > 0 ? start <= to : start >= to; start += step * size) {
      const end = step > 0 ? Math.min(start + size - 1, to) : Math.max(start - size + 1, to);
      lines.push(start === end ? String(start) : start + '-' + end);
      if (lines.length >= config.maxItems) break;
    }
    part.querySelector('textarea').value = lines.join('\n');
    sync();
  }

  function newPart(raw) {
    const part = template.content.firstElementChild.cloneNode(true);
    part.querySelector('textarea').value = raw || '';
    part.querySelector('textarea').addEventListener('input', sync);
    const numbers = part.querySelector('.word-list-numbers');
    const numbersToggle = part.querySelector('[data-action="numbers"]');
    numbersToggle.addEventListener('click', () => {
      numbers.hidden = !numbers.hidden;
      numbersToggle.setAttribute('aria-expanded', String(!numbers.hidden));
    });
    part.querySelector('[data-action="fill"]').addEventListener('click', () => fillNumbers(part));
    part.querySelector('[data-action="insert"]').addEventListener('click', () => {
      insertToken(token(parts().indexOf(part) + 1)); sync();
    });
    part.querySelector('[data-action="remove-part"]').addEventListener('click', () => {
      const number = parts().indexOf(part) + 1;
      dropToken(number);
      part.remove();
      if (!parts().length) closePanel(); else { sync(); parts()[Math.max(0, number - 2)].querySelector('textarea').focus(); }
    });
    partsBox.appendChild(part);
    return part;
  }

  function renumber() {
    parts().forEach((part, index) => {
      const number = index + 1, id = 'word_list_' + number, text = token(number);
      const textarea = part.querySelector('textarea');
      textarea.id = id;
      part.querySelector('[data-role="label"]').htmlFor = id;
      part.querySelector('[data-role="title"]').textContent = fillIn(labels.part, {number});
      part.querySelector('[data-role="token"]').textContent = text;
      part.querySelector('[data-role="missing"]').textContent = fillIn(labels.missing, {token: text});
      part.querySelector('[data-action="insert"]').textContent = fillIn(labels.insert, {token: text});
      part.querySelector('[data-action="remove-part"]').hidden = parts().length === 1;
      part.querySelector('.word-list-numbers').id = id + '_numbers';
      part.querySelector('[data-action="numbers"]').setAttribute('aria-controls', id + '_numbers');
    });
  }

  function previewItem(values) {
    const row = document.createElement('li'), body = document.createElement('span');
    let last = 0;
    const text = message.value;
    for (const match of text.matchAll(TOKENS)) {
      const number = parseInt(match[1] || '1', 10);
      body.appendChild(document.createTextNode(text.slice(last, match.index)));
      if (number >= 1 && number <= values.length) {
        const mark = document.createElement('mark'); mark.textContent = values[number - 1]; body.appendChild(mark);
      } else body.appendChild(document.createTextNode(match[0]));
      last = match.index + match[0].length;
    }
    body.appendChild(document.createTextNode(text.slice(last)));
    row.appendChild(body);
    return row;
  }

  function sync() {
    const series = mode && mode.value === 'series';
    add.hidden = series || !panel.hidden;
    if (series) { panel.hidden = true; return; }
    store.value = panel.hidden ? '[]' : JSON.stringify(parts().map(part => part.querySelector('textarea').value));
    if (panel.hidden) return;
    renumber();
    const all = lists(), mentioned = used(), total = longest();
    parts().forEach((part, index) => {
      const items = all[index];
      part.querySelector('.word-list-missing').hidden = mentioned.has(index + 1) || !items.length;
      part.querySelector('.word-list-count').textContent = items.length ? fillIn(labels.count, {count: items.length}) : labels.empty;
    });
    removeAll.textContent = parts().length > 1 ? labels.removeAll : labels.remove;
    addPart.hidden = parts().length >= config.maxParts;
    together.hidden = parts().length < 2;
    if (next) {
      const chosen = parseInt(next.value || next.dataset.initial || '0', 10) || 0;
      const options = [];
      for (let step = 0; step < total; step++) {
        const values = all.filter(items => items.length).map(items => items[step % items.length]);
        options.push(new Option(fillIn(labels.send, {number: step + 1}) + ': ' + values.join(' · '), String(step)));
      }
      next.replaceChildren(...options);
      next.value = String(Math.min(chosen, Math.max(total - 1, 0)));
    }
    const start = next ? parseInt(next.value || '0', 10) || 0 : 0;
    const ready = total && all.every(items => items.length);
    const rows = [];
    for (let send = 0; ready && send < 3; send++) {
      const step = endMode() === 'stop' ? start + send : (start + send) % total;
      if (step >= total) break;
      rows.push(previewItem(all.map(items => items[step % items.length])));
    }
    previewList.replaceChildren(...rows);
    previewList.hidden = !rows.length;
    const oneTime = trigger && ['now', 'date'].includes(trigger.value);
    if (!ready) after.textContent = labels.previewEmpty;
    else if (oneTime) after.textContent = labels.oneTime;
    else if (endMode() === 'stop') after.textContent = fillIn(labels.stops, {count: total - start});
    else if (all.length > 1) after.textContent = labels.repeatsAll;
    else after.textContent = fillIn(labels.repeats, {first: all[0][0]});
    changed();
  }

  function openPanel(raws) {
    panel.hidden = false;
    partsBox.replaceChildren();
    (raws.length ? raws : ['']).forEach(raw => newPart(raw));
  }

  function closePanel() {
    partsBox.replaceChildren();
    panel.hidden = true;
    sync();
    add.focus();
  }

  function saved() {
    try { const value = JSON.parse(store.value || '[]'); return Array.isArray(value) ? value.map(String) : []; }
    catch (error) { return []; }
  }

  add.addEventListener('click', () => {
    openPanel([]);
    if (!used().has(1)) insertToken(token(1));
    sync();
    parts()[0].querySelector('textarea').focus();
  });
  addPart.addEventListener('click', () => {
    const part = newPart('');
    const number = parts().length;
    if (!used().has(number)) insertToken(token(number));
    sync();
    part.querySelector('textarea').focus();
  });
  removeAll.addEventListener('click', () => {
    for (let number = parts().length; number >= 1; number--) dropToken(number);
    closePanel();
  });
  message.addEventListener('input', () => { if (!panel.hidden) sync(); });
  panel.querySelectorAll('[name="word_list_end"]').forEach(radio => radio.addEventListener('change', sync));
  if (next) next.addEventListener('change', sync);
  if (mode) mode.addEventListener('change', sync);
  if (trigger) trigger.addEventListener('change', sync);

  // A message that already has lists or placeholders (edit, failed submit) opens with them.
  function load() {
    const raws = saved().filter((raw, index, all) => raw.trim() || index < all.length - 1);
    if (raws.length || used().size) openPanel(raws.length ? raws : ['']);
    else { partsBox.replaceChildren(); panel.hidden = true; }
    sync();
  }
  load();

  return {
    refresh: load,
    validate() {
      if (panel.hidden || (mode && mode.value === 'series')) return true;
      const all = lists(), mentioned = used();
      for (let index = 0; index < all.length; index++) {
        const part = parts()[index];
        if (!all[index].length) { part.querySelector('textarea').focus(); return false; }
        if (!mentioned.has(index + 1)) {
          const warning = part.querySelector('.word-list-missing');
          warning.hidden = false; warning.scrollIntoView({behavior: 'smooth', block: 'center'}); return false;
        }
      }
      return true;
    }
  };
}
