(function () {
  const script = document.querySelector('script[data-connection-messages]');
  const messages = JSON.parse(script.dataset.connectionMessages);
  document.querySelectorAll('[data-connection-id]').forEach(async function (element) {
    try {
      const response = await fetch('/tokens/' + element.dataset.connectionId + '/whatsapp/status', {
        cache: 'no-store', signal: AbortSignal.timeout(10000)
      });
      if (!response.ok || response.redirected) throw new Error();
      const data = await response.json();
      element.textContent = messages[data.state] || messages.unknown;
    } catch (error) {
      element.textContent = messages.unknown;
    }
  });
})();
