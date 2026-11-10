(() => {
  const picker = document.getElementById('provider');
  if (!picker) return;
  const update = () => {
    const option = picker.selectedOptions[0];
    document.getElementById('credential-label').textContent = option.dataset.label;
    document.getElementById('credential-help').textContent = option.dataset.help;
  };
  picker.addEventListener('change', update);
  update();
})();
