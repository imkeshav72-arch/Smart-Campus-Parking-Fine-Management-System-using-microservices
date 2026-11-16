document.addEventListener('DOMContentLoaded', () => {
  const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];

  // mobile sidebar
  const mb = $('#menuBtn'); if (mb) mb.onclick = () => document.body.classList.toggle('nav-open');

  // flash close button
  $$('.flash button').forEach(b => b.onclick = () => b.parentElement.remove());

  // confirm dialogs: <form data-confirm="Are you sure?">
  $$('[data-confirm]').forEach(f => f.addEventListener('submit', e => { if (!confirm(f.dataset.confirm)) e.preventDefault(); }));

  // highlight chosen parking slot
  $$('.slot input').forEach(r => r.addEventListener('change', () => {
    $$('.slot').forEach(s => s.classList.remove('selected'));
    r.closest('.slot').classList.add('selected');
    const c = $('#chosen'); if (c) c.textContent = 'Selected slot: ' + r.closest('.slot').querySelector('b').textContent;
  }));

  // login page: click a demo account to fill the form
  $$('[data-demo]').forEach(b => b.onclick = () => {
    $('[name=email]').value = b.dataset.email; $('[name=password]').value = b.dataset.pass;
    const r = $(`input[name=role][value=${b.dataset.role}]`); if (r) r.checked = true;
  });

  // fake Razorpay payment method tabs
  $$('.pm-tab').forEach(t => t.onclick = () => {
    $$('.pm-tab').forEach(x => x.classList.remove('active')); $$('.pm-panel').forEach(x => x.classList.remove('active'));
    t.classList.add('active'); $('#' + t.dataset.target).classList.add('active'); $('#methodInput').value = t.dataset.method;
  });
  const pf = $('#payForm');
  if (pf) pf.addEventListener('submit', () => { const b = $('#payBtn'); b.disabled = true; b.textContent = 'Processing…'; });

  // QR scanning for guards (camera or uploaded image) using jsQR
  const submitCode = txt => { $('#codeInput').value = txt; $('#verifyForm').submit(); };
  const sb = $('#startScan');
  if (sb) sb.onclick = async () => {
    if (!window.jsQR) return alert('Scanner library did not load (internet needed). Type the booking code instead.');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
      const v = $('#scanVideo'); v.srcObject = stream; v.hidden = false; v.setAttribute('playsinline', true); await v.play();
      const cv = document.createElement('canvas'), cx = cv.getContext('2d');
      const tick = () => {
        if (v.readyState === v.HAVE_ENOUGH_DATA) {
          cv.width = v.videoWidth; cv.height = v.videoHeight; cx.drawImage(v, 0, 0);
          const im = cx.getImageData(0, 0, cv.width, cv.height), res = jsQR(im.data, im.width, im.height);
          if (res && res.data) { stream.getTracks().forEach(t => t.stop()); return submitCode(res.data); }
        }
        requestAnimationFrame(tick);
      };
      tick();
    } catch (e) { alert('Camera not available. Type the booking code or upload the QR image.'); }
  };
  const qf = $('#qrFile');
  if (qf) qf.onchange = () => {
    if (!window.jsQR) return alert('Scanner library did not load. Type the booking code instead.');
    const img = new Image();
    img.onload = () => {
      const cv = document.createElement('canvas'); cv.width = img.width; cv.height = img.height;
      const cx = cv.getContext('2d'); cx.drawImage(img, 0, 0);
      const im = cx.getImageData(0, 0, cv.width, cv.height), res = jsQR(im.data, im.width, im.height);
      res ? submitCode(res.data) : alert('No QR code found in that image.');
    };
    img.src = URL.createObjectURL(qf.files[0]);
  };
});
