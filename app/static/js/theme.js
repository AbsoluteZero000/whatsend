(function () {
  const root = document.documentElement;

  function isDark() {
    return root.getAttribute('data-theme') === 'dark';
  }

  function sync() {
    document.querySelectorAll('.theme-switch').forEach(function (button) {
      button.setAttribute('aria-checked', isDark() ? 'true' : 'false');
    });
  }

  document.querySelectorAll('.theme-switch').forEach(function (button) {
    button.addEventListener('click', function () {
      const theme = isDark() ? 'light' : 'dark';
      root.setAttribute('data-theme', theme);
      try { localStorage.setItem('theme', theme); } catch (e) {}
      sync();
    });
  });
  sync();
})();
