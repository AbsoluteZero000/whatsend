(function () {
  var meta = document.querySelector('meta[name="csrf-token"]');
  if (!meta) return;
  var token = meta.content;

  function protectForm(form) {
    if ((form.method || 'get').toLowerCase() !== 'post') return;
    var input = form.querySelector('input[name="csrf_token"]');
    if (!input) {
      input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'csrf_token';
      form.appendChild(input);
    }
    input.value = token;
  }

  document.querySelectorAll('form').forEach(protectForm);
  document.addEventListener('submit', function (event) { protectForm(event.target); }, true);
  window.csrfToken = token;
})();
