(() => {
  const form = document.getElementById('tiktok-publish-form');
  const kind = document.getElementById('content_kind');
  const video = document.getElementById('video');
  const preview = document.getElementById('tiktok-video-preview');
  const commercial = document.getElementById('commercial');
  const own = document.getElementById('own_brand');
  const branded = document.getElementById('branded');
  const privacy = document.getElementById('privacy_level');
  const button = document.getElementById('tiktok-publish-button');
  let previewUrl;
  function update() {
    const photo = kind.value === 'photo';
    document.getElementById('tiktok-video-fields').hidden = photo;
    document.getElementById('tiktok-photo-fields').hidden = !photo;
    document.getElementById('tiktok-video-interactions').hidden = photo;
    video.disabled = photo;
    video.required = !photo;
    for (const id of ['photos', 'photo_title']) document.getElementById(id).disabled = !photo;
    document.getElementById('photos').required = photo;
    for (const name of ['allow_duet', 'allow_stitch']) {
      const input = form.elements[name];
      if (!input.dataset.creatorDisabled) input.dataset.creatorDisabled = input.disabled ? 'yes' : 'no';
      input.disabled = photo || input.dataset.creatorDisabled === 'yes';
      if (photo) input.checked = false;
    }
    document.getElementById('caption').maxLength = photo ? 4000 : 2200;
    document.getElementById('tiktok-commercial-fields').hidden = !commercial.checked;
    if (!commercial.checked) own.checked = branded.checked = false;
    for (const option of privacy.options) {
      if (option.value === 'SELF_ONLY') option.disabled = branded.checked;
    }
    if (branded.checked && privacy.value === 'SELF_ONLY') privacy.value = '';
    const needsDisclosure = commercial.checked && !own.checked && !branded.checked;
    button.disabled = needsDisclosure;
    document.getElementById('tiktok-disclosure').textContent = needsDisclosure
      ? 'Choose Your brand, Branded content, or both before publishing.'
      : branded.checked ? 'This post will be labeled Paid partnership. Private visibility is unavailable.'
      : own.checked ? 'This post will be labeled Promotional content.' : '';
  }
  for (const input of [kind, commercial, own, branded, privacy]) input.addEventListener('change', update);
  video.addEventListener('change', () => {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    preview.hidden = !video.files.length;
    if (video.files.length) { previewUrl = URL.createObjectURL(video.files[0]); preview.src = previewUrl; }
  });
  form.addEventListener('submit', () => { button.disabled = true; button.textContent = 'Submitting…'; });
  update();
})();
