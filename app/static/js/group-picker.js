/* Searchable recipient picker; the select remains the source of group data. */
window.createGroupPicker = function(select, selected) {
  const root = document.getElementById('group_search_picker');
  const input = root.querySelector('input');
  const panel = root.querySelector('.group-search-panel');
  const list = root.querySelector('[role="listbox"]');
  const note = root.querySelector('[role="status"]');
  let active = -1, rows = [];
  function open(value) {
    panel.hidden = !value;
    input.setAttribute('aria-expanded', String(value));
    if (!value) { active = -1; input.removeAttribute('aria-activedescendant'); }
  }
  function highlight(index) {
    active = index;
    rows.forEach((row, i) => row.classList.toggle('is-active', i === active));
    if (rows[active]) {
      input.setAttribute('aria-activedescendant', rows[active].id);
      rows[active].scrollIntoView({block: 'nearest'});
    } else input.removeAttribute('aria-activedescendant');
  }
  function refresh() {
    const query = input.value.trim().toLocaleLowerCase();
    const ids = new Set(selected().map(g => g.id));
    list.replaceChildren(); rows = []; active = -1;
    input.removeAttribute('aria-activedescendant');
    const options = Array.from(select.options).filter(o => o.value && o.value !== '__manual__' &&
      (o.textContent + ' ' + o.value).toLocaleLowerCase().includes(query));
    options.sort((a, b) => a.textContent.localeCompare(b.textContent));
    for (const option of options) {
      const row = document.createElement('div');
      row.className = 'group-search-option'; row.id = 'group-option-' + rows.length;
      row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', String(ids.has(option.value)));
      const icon = document.createElement('span'); icon.className = 'group-search-icon';
      icon.textContent = ids.has(option.value) ? '✓' : '+'; icon.setAttribute('aria-hidden', 'true');
      const text = document.createElement('span'); text.className = 'group-search-text';
      const name = document.createElement('strong'); name.textContent = option.textContent;
      const id = document.createElement('small'); id.textContent = option.value; id.dir = 'ltr';
      text.append(name, id); row.append(icon, text);
      row.addEventListener('mousedown', e => e.preventDefault());
      row.addEventListener('click', () => choose(option.value));
      row.dataset.value = option.value; rows.push(row); list.append(row);
    }
    input.setAttribute('aria-busy', String(select.dataset.state === 'loading'));
    note.textContent = select.dataset.state === 'loading' ? root.dataset.loading :
      select.dataset.state === 'error' ? root.dataset.unavailable :
      options.length ? options.length + ' ' + root.dataset.results : root.dataset.empty;
  }
  function choose(value) {
    select.value = value;
    select.dispatchEvent(new Event('change', {bubbles: true}));
    input.value = ''; refresh();
    if (value === '__manual__') open(false);
    else { input.focus(); open(true); }
  }
  input.addEventListener('focus', () => { refresh(); open(true); });
  input.addEventListener('click', () => { if (panel.hidden) { refresh(); open(true); } });
  input.addEventListener('input', () => { refresh(); open(true); });
  input.addEventListener('keydown', e => {
    if (e.key === 'Escape') { e.preventDefault(); open(false); }
    else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault(); open(true);
      if (rows.length) highlight((active + (e.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length);
    } else if (e.key === 'Enter') {
      e.preventDefault(); if (!panel.hidden && rows[active]) choose(rows[active].dataset.value);
    }
  });
  root.querySelector('.group-manual-action').addEventListener('click', () => choose('__manual__'));
  document.addEventListener('pointerdown', e => { if (!root.contains(e.target)) open(false); });
  root.addEventListener('focusout', () => { setTimeout(() => { if (!root.contains(document.activeElement)) open(false); }, 0); });
  new MutationObserver(refresh).observe(select, {childList: true, attributes: true, attributeFilter: ['data-state']});
  refresh();
  return {refresh};
};
