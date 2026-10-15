// Searchable timezone picker. Enhances <input class="timezone-picker" list="..."> backed by a
// <datalist> of IANA names: the visible field searches, a hidden input submits the IANA value.
(function () {
  const REGIONS = ['Africa', 'America', 'Antarctica', 'Asia', 'Atlantic', 'Australia', 'Europe', 'Indian', 'Pacific'];
  const MAX_RESULTS = 80;

  function offsetMinutes(tz, now) {
    try {
      const name = new Intl.DateTimeFormat('en-US', {timeZone: tz, timeZoneName: 'longOffset'})
        .formatToParts(now).find(part => part.type === 'timeZoneName').value;
      const match = /GMT([+-])(\d{2}):?(\d{2})?/.exec(name);
      if (!match) return 0;
      return (match[1] === '-' ? -1 : 1) * (Number(match[2]) * 60 + Number(match[3] || 0));
    } catch (e) {
      return null;
    }
  }

  function offsetLabel(minutes) {
    if (minutes === null) return '';
    if (minutes === 0) return 'UTC';
    const sign = minutes < 0 ? '−' : '+';
    const abs = Math.abs(minutes);
    const hours = Math.floor(abs / 60);
    const mins = abs % 60;
    return 'UTC' + sign + hours + (mins ? ':' + String(mins).padStart(2, '0') : '');
  }

  function describe(tz, now, locale) {
    const parts = tz.split('/');
    const city = parts[parts.length - 1].replace(/_/g, ' ');
    const region = parts.length > 1 ? parts.slice(0, -1).join(' / ').replace(/_/g, ' ') : '';
    const minutes = offsetMinutes(tz, now);
    let time = '';
    try {
      time = new Intl.DateTimeFormat(locale, {timeZone: tz, hour: '2-digit', minute: '2-digit', hour12: false}).format(now);
    } catch (e) {}
    const offset = offsetLabel(minutes);
    return {
      tz, city, region, minutes, offset, time,
      search: (tz + ' ' + city + ' ' + region + ' ' + offset + ' ' + offset.replace('−', '-')).toLowerCase(),
    };
  }

  function enhance(input) {
    const list = input.list || document.getElementById(input.getAttribute('list'));
    if (!list) return;
    const all = Array.from(list.options, option => option.value);
    const locale = document.documentElement.lang === 'ar' ? 'ar-EG' : 'en-GB';
    const text = {
      empty: input.dataset.emptyText || 'No matching timezones',
      detected: input.dataset.detectedText || 'Detected',
    };
    let detected = '';
    try { detected = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) {}

    // The hidden field carries the IANA name; the visible field becomes a search box.
    const hidden = document.createElement('input');
    hidden.type = 'hidden';
    hidden.name = input.name;
    hidden.value = all.includes(input.value) ? input.value : '';
    input.removeAttribute('name');
    input.removeAttribute('list');
    input.required = false;
    input.after(hidden);

    const wrap = document.createElement('div');
    wrap.className = 'tz-combobox';
    input.before(wrap);
    wrap.appendChild(input);
    const panel = document.createElement('ul');
    panel.className = 'tz-panel';
    panel.id = input.id + '-listbox';
    panel.setAttribute('role', 'listbox');
    panel.hidden = true;
    wrap.appendChild(panel);
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('aria-controls', panel.id);

    let entries = [];
    let results = [];
    let active = -1;

    function build() {
      const now = new Date();
      const visible = all.filter(tz => tz === 'UTC' || tz === hidden.value || tz === detected || REGIONS.includes(tz.split('/')[0]));
      entries = visible.map(tz => describe(tz, now, locale));
      entries.sort((a, b) => (a.minutes ?? 0) - (b.minutes ?? 0) || a.city.localeCompare(b.city));
    }

    function labelFor(tz) {
      const entry = entries.find(item => item.tz === tz) || describe(tz, new Date(), locale);
      return entry.city + (entry.region ? ', ' + entry.region : '') + (entry.offset ? ' (' + entry.offset + ')' : '');
    }

    function render() {
      const query = input.value.trim().toLowerCase();
      const showingSelection = query === labelFor(hidden.value).toLowerCase();
      const terms = showingSelection ? [] : query.split(/\s+/).filter(Boolean);
      results = entries.filter(entry => terms.every(term => entry.search.includes(term)));
      if (terms.length) {
        // Prefer cities that start with the query, then word starts, then any match.
        const first = terms[0];
        const score = entry => {
          const city = entry.city.toLowerCase();
          if (city.startsWith(first)) return 0;
          if (city.split(/[\s-]+/).some(word => word.startsWith(first))) return 1;
          if (entry.region.toLowerCase().startsWith(first)) return 2;
          return 3;
        };
        results = results.map(entry => [score(entry), entry]).sort((a, b) => a[0] - b[0]).map(pair => pair[1]);
      } else {
        // Put the detected and current timezones first when nothing is typed.
        const pinned = [hidden.value, detected].filter(Boolean);
        results = [...results.filter(e => pinned.includes(e.tz)), ...results.filter(e => !pinned.includes(e.tz))];
      }
      results = results.slice(0, MAX_RESULTS);
      panel.replaceChildren();
      if (!results.length) {
        const empty = document.createElement('li');
        empty.className = 'tz-empty';
        empty.textContent = text.empty;
        panel.appendChild(empty);
      }
      results.forEach((entry, index) => {
        const item = document.createElement('li');
        item.id = panel.id + '-' + index;
        item.className = 'tz-option';
        item.setAttribute('role', 'option');
        item.setAttribute('aria-selected', entry.tz === hidden.value ? 'true' : 'false');
        const main = document.createElement('span');
        main.className = 'tz-option-main';
        const city = document.createElement('strong');
        city.textContent = entry.city;
        const region = document.createElement('small');
        region.textContent = entry.region || entry.tz;
        main.append(city, region);
        if (entry.tz === detected) {
          const tag = document.createElement('span');
          tag.className = 'tz-tag';
          tag.textContent = text.detected;
          city.appendChild(tag);
        }
        const meta = document.createElement('span');
        meta.className = 'tz-option-meta';
        const time = document.createElement('strong');
        time.textContent = entry.time;
        const offset = document.createElement('small');
        offset.textContent = entry.offset;
        meta.append(time, offset);
        item.append(main, meta);
        item.addEventListener('mousedown', function (event) {
          event.preventDefault();
          choose(entry.tz);
        });
        panel.appendChild(item);
      });
      setActive(results.findIndex(entry => entry.tz === hidden.value));
    }

    function setActive(index) {
      const items = panel.querySelectorAll('.tz-option');
      items.forEach(item => item.classList.remove('is-active'));
      active = index;
      if (index >= 0 && items[index]) {
        items[index].classList.add('is-active');
        input.setAttribute('aria-activedescendant', items[index].id);
        items[index].scrollIntoView({block: 'nearest'});
      } else {
        input.removeAttribute('aria-activedescendant');
      }
    }

    function open() {
      if (!entries.length) build();
      render();
      panel.hidden = false;
      input.setAttribute('aria-expanded', 'true');
    }

    function close() {
      panel.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
    }

    function choose(tz) {
      hidden.value = tz;
      input.value = labelFor(tz);
      input.setCustomValidity('');
      close();
    }

    input.addEventListener('focus', function () {
      open();
      input.select();
    });
    input.addEventListener('input', function () {
      if (panel.hidden) open(); else render();
      setActive(results.length ? 0 : -1);
    });
    input.addEventListener('keydown', function (event) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (panel.hidden) { open(); return; }
        const step = event.key === 'ArrowDown' ? 1 : -1;
        setActive(Math.max(0, Math.min(results.length - 1, active + step)));
      } else if (event.key === 'Enter' && !panel.hidden) {
        event.preventDefault();
        if (results[active]) choose(results[active].tz);
      } else if (event.key === 'Escape' && !panel.hidden) {
        event.preventDefault();
        close();
        input.value = hidden.value ? labelFor(hidden.value) : '';
      }
    });
    input.addEventListener('blur', function () {
      close();
      input.value = hidden.value ? labelFor(hidden.value) : '';
    });
    input.form.addEventListener('submit', function (event) {
      if (!hidden.value) {
        event.preventDefault();
        input.setCustomValidity(text.empty);
        input.reportValidity();
      }
    });

    if (!hidden.value) hidden.value = all.includes(detected) ? detected : (all.includes('UTC') ? 'UTC' : '');
    build();
    if (hidden.value) input.value = labelFor(hidden.value);
  }

  document.querySelectorAll('input.timezone-picker[list]').forEach(enhance);
})();
