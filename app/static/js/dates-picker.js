// Calendar for "On specific dates": tap days to pick them, move between months with the arrows.
// Picked days are kept in the hidden #schedule_dates input as "YYYY-MM-DD,YYYY-MM-DD" (user's timezone).
(function () {
  const root = document.getElementById('trigger_value_dates');
  if (!root) return;
  const input = document.getElementById('schedule_dates');
  const calendar = root.querySelector('.dates-calendar');
  const selectedBox = root.querySelector('.dates-selected');
  const error = document.getElementById('dates_error');
  const labels = JSON.parse(root.dataset.labels || '{}');
  const max = Number(root.dataset.max) || 100;
  const arabic = root.dataset.lang === 'ar';
  const locale = arabic ? 'ar-EG' : 'en-GB';
  const weekStart = arabic ? 6 : 0; // Saturday in Arabic, Sunday in English
  const today = root.dataset.today || new Date().toISOString().slice(0, 10);
  const MAX_MONTHS_AHEAD = 24;
  let picked = new Set();
  let view = null;
  let notice = '';

  const pad = n => String(n).padStart(2, '0');
  const keyOf = (year, month, day) => year + '-' + pad(month + 1) + '-' + pad(day);
  const dateOf = key => { const [y, m, d] = key.split('-').map(Number); return new Date(Date.UTC(y, m - 1, d)); };
  const format = (date, options) => new Intl.DateTimeFormat(locale, {timeZone: 'UTC', ...options}).format(date);
  const todayDate = dateOf(today);
  const monthIndex = (year, month) => year * 12 + month;
  const firstMonth = monthIndex(todayDate.getUTCFullYear(), todayDate.getUTCMonth());

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function save() {
    input.value = [...picked].sort().join(',');
    input.dispatchEvent(new Event('change', {bubbles: true}));
    if (picked.size) error.hidden = true;
  }

  function toggle(key) {
    notice = '';
    if (picked.has(key)) picked.delete(key);
    else if (picked.size >= max) notice = labels.limit;
    else picked.add(key);
    save();
    render();
  }

  function goTo(year, month) {
    const index = Math.min(Math.max(monthIndex(year, month), firstMonth), firstMonth + MAX_MONTHS_AHEAD);
    view = {year: Math.floor(index / 12), month: index % 12};
    render();
  }

  function renderMonth() {
    const {year, month} = view;
    const header = node('div', 'dates-calendar-header');
    const previous = node('button', 'dates-nav', arabic ? '›' : '‹');
    previous.type = 'button';
    previous.setAttribute('aria-label', labels.previous);
    previous.disabled = monthIndex(year, month) <= firstMonth;
    previous.addEventListener('click', () => goTo(year, month - 1));
    const next = node('button', 'dates-nav', arabic ? '‹' : '›');
    next.type = 'button';
    next.setAttribute('aria-label', labels.next);
    next.disabled = monthIndex(year, month) >= firstMonth + MAX_MONTHS_AHEAD;
    next.addEventListener('click', () => goTo(year, month + 1));
    const title = node('strong', 'dates-month', format(new Date(Date.UTC(year, month, 1)), {month: 'long', year: 'numeric'}));
    const prefix = year + '-' + pad(month + 1) + '-';
    const inMonth = [...picked].filter(key => key.startsWith(prefix)).length;
    const titleWrap = node('div', 'dates-month-wrap');
    titleWrap.append(title);
    if (inMonth) titleWrap.append(node('small', 'dates-month-count', inMonth + ' ' + labels.inMonth));
    header.append(previous, titleWrap, next);

    const grid = node('div', 'dates-grid');
    grid.setAttribute('role', 'group');
    grid.setAttribute('aria-label', title.textContent);
    for (let i = 0; i < 7; i++) {
      // 2023-01-01 was a Sunday.
      const weekday = new Date(Date.UTC(2023, 0, 1 + (weekStart + i) % 7));
      const cell = node('span', 'dates-weekday', format(weekday, {weekday: arabic ? 'narrow' : 'short'}));
      cell.setAttribute('aria-hidden', 'true');
      grid.append(cell);
    }
    const first = new Date(Date.UTC(year, month, 1)).getUTCDay();
    for (let i = 0; i < (first - weekStart + 7) % 7; i++) grid.append(node('span', 'dates-blank'));
    const days = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
    for (let day = 1; day <= days; day++) {
      const key = keyOf(year, month, day);
      const button = node('button', 'dates-day', format(dateOf(key), {day: 'numeric'}));
      button.type = 'button';
      button.dataset.date = key;
      button.setAttribute('aria-label', format(dateOf(key), {weekday: 'long', day: 'numeric', month: 'long', year: 'numeric'}));
      button.setAttribute('aria-pressed', picked.has(key) ? 'true' : 'false');
      if (key === today) button.classList.add('is-today');
      if (key < today) button.disabled = true;
      button.addEventListener('click', () => toggle(key));
      grid.append(button);
    }
    calendar.replaceChildren(header, grid);
  }

  function renderSelected() {
    const keys = [...picked].sort();
    const summary = node('div', 'dates-summary');
    summary.append(node('strong', '', keys.length
      ? keys.length + ' ' + (keys.length === 1 ? labels.onePicked : labels.picked)
      : labels.none));
    if (keys.length) {
      const clear = node('button', 'btn-clear', labels.clear);
      clear.type = 'button';
      clear.addEventListener('click', () => { picked.clear(); notice = ''; save(); render(); });
      summary.append(clear);
    }
    const chips = node('div', 'dates-chips');
    keys.forEach(key => {
      const date = dateOf(key);
      const chip = node('span', 'dates-chip');
      const jump = node('button', 'dates-chip-label', format(date, {weekday: 'short', day: 'numeric', month: 'short', year: date.getUTCFullYear() === todayDate.getUTCFullYear() ? undefined : 'numeric'}));
      jump.type = 'button';
      jump.addEventListener('click', () => goTo(date.getUTCFullYear(), date.getUTCMonth()));
      const remove = node('button', 'dates-chip-remove', '×');
      remove.type = 'button';
      remove.setAttribute('aria-label', labels.remove + ' ' + format(date, {day: 'numeric', month: 'long', year: 'numeric'}));
      remove.addEventListener('click', () => toggle(key));
      chip.append(jump, remove);
      chips.append(chip);
    });
    selectedBox.replaceChildren(summary, chips);
    if (notice) selectedBox.append(node('p', 'form-error', notice));
  }

  function render() {
    renderMonth();
    renderSelected();
  }

  function refresh() {
    // Past days cannot be scheduled, so they are dropped when the form is reopened.
    picked = new Set((input.value || '').split(',').map(value => value.trim())
      .filter(value => /^\d{4}-\d{2}-\d{2}$/.test(value) && value >= today));
    const firstPicked = [...picked].sort()[0];
    const start = dateOf(firstPicked || today);
    view = null;
    goTo(start.getUTCFullYear(), start.getUTCMonth());
    if (input.value !== [...picked].sort().join(',')) save();
  }

  window.datesPicker = {refresh};
  refresh();
})();
