const MB         = 1024 * 1024;
const CPU_PERIOD = 100000;
const MINUTE     = 60;

const _meta          = document.getElementById('admin-meta');
const CSRF           = _meta.dataset.csrf;
const _uploadCertUrl = _meta.dataset.uploadCertUrl;
const _deleteCertUrl = _meta.dataset.deleteCertUrl;
const _saveConfigUrl = _meta.dataset.saveConfigUrl;

/* ── Worker nodes ────────────────────────────────────────────────────── */
document.getElementById('add-node-btn').addEventListener('click', () => {
  const container = document.getElementById('worker-nodes-list');
  const div = document.createElement('div');
  div.className = 'input-group mb-1 worker-node-entry';
  div.style.maxWidth = '50%';
  div.innerHTML = `
    <input type="text" class="form-control worker-node-input" placeholder="user@host">
    <button type="button" class="btn btn-danger remove-node-btn">X</button>
  `;
  container.appendChild(div);
  div.querySelector('.remove-node-btn').addEventListener('click', () => div.remove());
});

document.querySelectorAll('.remove-node-btn').forEach(btn => {
  btn.addEventListener('click', e => e.target.closest('.worker-node-entry').remove());
});

/* ── Certificate upload ──────────────────────────────────────────────── */
const certInput  = document.getElementById('cert-file-input');
const certBtn    = document.getElementById('cert-upload-btn');
const certStatus = document.getElementById('cert-upload-status');

function setCertStatus(msg, type) {
  certStatus.innerHTML = `<small class="text-${type}">${msg}</small>`;
}

certInput?.addEventListener('change', () => {
  certBtn.disabled = !certInput.files.length;
  certStatus.innerHTML = '';
});

function uploadCert() {
  const file = certInput.files[0];
  if (!file) return;

  certBtn.disabled = true;
  setCertStatus('Uploading…', 'info');

  const fd = new FormData();
  fd.append('cert', file);
  fd.append('nonce', CSRF);

  fetch(_uploadCertUrl, {
    method: 'POST',
    headers: { 'CSRF-Token': CSRF, 'X-CSRFToken': CSRF },
    body: fd,
  })
  .then(r => r.json())
  .then(data => {
    if (data.success) {
      setCertStatus('✓ Certificate uploaded. Reloading…', 'success');
      setTimeout(() => location.reload(), 800);
    } else {
      setCertStatus('Failed: ' + (data.error || 'unknown error'), 'danger');
      certBtn.disabled = false;
    }
  })
  .catch(err => {
    setCertStatus('Network error: ' + err, 'danger');
    certBtn.disabled = false;
  });
}

function deleteCert() {
  if (!confirm('Remove the registry TLS certificate?')) return;
  fetch(_deleteCertUrl, {
    method: 'POST',
    headers: { 'CSRF-Token': CSRF, 'Content-Type': 'application/json' },
  })
  .then(r => r.json())
  .then(data => {
    if (data.success) location.reload();
    else alert('Failed: ' + (data.error || 'unknown'));
  });
}

/* ── Save config ─────────────────────────────────────────────────────── */
async function saveConfig() {
  const btns   = document.querySelectorAll('#save-btn, #save-btn-bottom');
  const status = document.getElementById('save-status');

  btns.forEach(b => b.disabled = true);
  status.textContent = 'Saving…';

  try {
    const data = {};

    document.querySelectorAll('input[name]').forEach(input => {
      data[input.name] = input.value;
    });

    document.querySelectorAll('.memory-mb').forEach(input => {
      data[input.dataset.bytesField] = Math.max(0, parseInt(input.value || 0)) * MB;
    });

    document.querySelectorAll('.cpu-cores').forEach(input => {
      data[input.dataset.quotaField] = Math.round(Math.max(0, parseFloat(input.value || 0)) * CPU_PERIOD);
    });

    document.querySelectorAll('.minutes-field').forEach(input => {
      data[input.dataset.secondsField] = Math.max(0, parseInt(input.value || 0)) * MINUTE;
    });

    const workerNodes = [];
    document.querySelectorAll('.worker-node-input').forEach(input => {
      const val = input.value.trim();
      if (val) workerNodes.push(val);
    });
    data['docker_worker_nodes'] = workerNodes;

    const resp = await fetch(_saveConfigUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'CSRF-Token': CSRF },
      body: JSON.stringify(data),
    });

    const result = await resp.json();
    if (result?.success) {
      status.textContent = 'Saved.';
      setTimeout(() => location.reload(), 600);
    } else {
      alert('Error saving settings: ' + (result?.error || 'Unknown error'));
      btns.forEach(b => b.disabled = false);
      status.textContent = '';
    }
  } catch (e) {
    alert('Network error: ' + e);
    btns.forEach(b => b.disabled = false);
    status.textContent = '';
  }
}
