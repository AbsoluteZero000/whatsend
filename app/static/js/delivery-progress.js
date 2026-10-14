(function() {
  const root = document.getElementById('delivery_progress');
  if (!root) return;
  const labels = JSON.parse(root.dataset.labels);
  const summary = root.querySelector('[role="status"]');
  const bar = root.querySelector('progress');
  const counts = root.querySelector('.delivery-counts');
  const list = root.querySelector('tbody');
  const error = root.querySelector('.delivery-refresh-note');
  let timer, refreshing = false, previousRecipients = '';
  async function refresh() {
    clearTimeout(timer);
    if (document.hidden || refreshing) return;
    refreshing = true;
    let delay = 5000;
    try {
      const response = await fetch(root.dataset.url, {cache:'no-store'});
      if (!response.ok || response.redirected) throw new Error();
      const data = await response.json();
      error.textContent = '';
      if (data.series) {
        document.querySelectorAll('[data-series-edit]').forEach(link => { link.hidden = data.series.cursor > 0; });
        if (data.series.cursor >= data.series.total && !data.series.active) {
          document.querySelectorAll('.detail-actions form').forEach(form => {
            if (/\/(pause|resume|skip|skip-clear|cancel|send-now)$/.test(new URL(form.action).pathname)) form.hidden = true;
          });
        }
        const position = document.getElementById('series_position');
        if (position) { position.textContent = data.series.cursor + ' / ' + data.series.total; position.className = 'badge badge-' + (data.series.cursor >= data.series.total && !data.series.active ? data.job_status : 'active'); }
        for (const entry of data.series.entries) {
          const badge = document.querySelector('[data-series-entry="' + entry.position + '"]');
          if (badge) { badge.textContent = labels[entry.status] || entry.status; badge.className = 'badge badge-' + (entry.status === 'sent' ? 'completed' : entry.status); }
        }
      }
      summary.textContent = data.total ? data.counts.sent + ' / ' + data.total + ' ' + labels.sent : labels.waiting;
      bar.max = data.total || 1;
      bar.value = data.counts.sent;
      bar.hidden = !data.total;
      counts.replaceChildren();
      for (const state of ['sent','pending','failed','unknown']) {
        const badge = document.createElement('span');
        badge.className = 'badge badge-' + (state === 'sent' ? 'completed' : state);
        badge.textContent = data.counts[state] + ' ' + labels[state]; counts.append(badge);
      }
      const recipientSignature = JSON.stringify(data.recipients);
      const scroll = root.querySelector('.table-wrap').scrollTop;
      const rows = document.createDocumentFragment();
      if (recipientSignature !== previousRecipients) for (const recipient of data.recipients) {
        const row = document.createElement('tr');
        const name = document.createElement('td'); name.textContent = recipient.name;
        const state = document.createElement('td');
        const badge = document.createElement('span');
        badge.className = 'badge badge-' + (recipient.status === 'sent' ? 'completed' : recipient.status);
        badge.textContent = labels[recipient.status] || recipient.status;
        state.append(badge);
        const note = document.createElement('td'); note.textContent = recipient.error || '—';
        row.append(name, state, note); rows.append(row);
      }
      if (recipientSignature !== previousRecipients) {
        list.replaceChildren(rows); previousRecipients = recipientSignature;
        root.querySelector('.table-wrap').scrollTop = scroll;
      }
      root.querySelector('.table-wrap').hidden = !data.total;
      document.querySelectorAll('[data-job-status]').forEach(badge => {
        badge.textContent = labels[data.job_status] || data.job_status;
        badge.className = 'badge badge-' + data.job_status;
      });
      if (data.total && !data.counts.pending && (['now','date'].includes(data.trigger_type) || (data.series && data.series.cursor >= data.series.total && !data.series.active)) && !data.counts.unknown) delay = 0;
      else if (data.counts.unknown && !data.counts.pending) delay = 15000;
    } catch { error.textContent = labels.error; delay = 10000; }
    finally {
      refreshing = false;
      if (delay) timer = setTimeout(refresh, delay);
    }
  }
  root.querySelector('[data-action="refresh"]').addEventListener('click', refresh);
  document.addEventListener('visibilitychange', () => { if (document.hidden) clearTimeout(timer); else refresh(); });
  refresh();
})();
