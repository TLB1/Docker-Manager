(function () {
  const list        = document.getElementById('containers-list');
  const addBtn      = document.getElementById('add-container-btn');
  const noMsg       = document.getElementById('no-containers-msg');
  const jsonField   = document.getElementById('docker_containers_json');
  const cardTpl     = document.getElementById('container-card-tpl');
  const portTpl     = document.getElementById('port-row-tpl');
  const meta        = document.getElementById('create-meta');
  const uploadUrl   = meta.dataset.uploadUrl;
  const imagesUrl   = meta.dataset.imagesUrl;

  let containerSeq = 0;

  // ── Global registry image cache ─────────────────────────────────────
  let registryImages  = [];   // [{tag, id, size_mb, node}]
  let registryFetched = false;
  let registryLoading = false;

  async function fetchRegistryImages() {
    if (registryLoading) return;
    registryLoading = true;
    try {
      const res  = await fetch(imagesUrl, { headers: { 'CSRF-Token': init.csrfNonce } });
      const data = await res.json();
      if (data.success) {
        registryImages  = data.images;
        registryFetched = true;
      }
    } catch (_) {}
    registryLoading = false;
    // Re-populate every visible registry dropdown
    list.querySelectorAll('.source-panel-registry').forEach(panel => {
      if (panel.style.display !== 'none') populateSelect(panel.querySelector('.container-image-name'));
    });
  }

  function populateSelect(select) {
    const current = select.value;
    select.innerHTML = '';
    if (!registryFetched) {
      select.innerHTML = '<option value="">— loading… —</option>';
      return;
    }
    if (!registryImages.length) {
      select.innerHTML = '<option value="">— no images found —</option>';
      return;
    }
    const blank = document.createElement('option');
    blank.value = '';
    blank.textContent = '— select an image —';
    select.appendChild(blank);
    registryImages.forEach(img => {
      const opt = document.createElement('option');
      opt.value = img.tag;
      opt.textContent = `${img.tag}  (${img.size_mb} MB)`;
      if (img.tag === current) opt.selected = true;
      select.appendChild(opt);
    });
  }

  // ── Serialise all cards → hidden JSON field ──────────────────────────
  function syncJson() {
    const data = [...list.querySelectorAll('.container-card')].map(card => {
      const source = card.querySelector('.source-tab.active')?.dataset.source ?? 'tar';

      const portRows = [...card.querySelectorAll('.port-row')].map(row => ({
        container_port: parseInt(row.querySelector('.port-number').value, 10) || null,
        label:          row.querySelector('.port-label').value.trim() || null,
        http:           row.querySelector('.port-http-value').value !== 'false',
      })).filter(p => p.container_port);

      return {
        index:                 parseInt(card.dataset.index, 10),
        label:                 card.querySelector('.container-label').value.trim() || null,
        port_mappings:         portRows,
        docker_image_filename: source === 'tar'
                                 ? (card.querySelector('.container-image-filename').value || null)
                                 : null,
        docker_image_name:     source === 'registry'
                                 ? (card.querySelector('.container-image-name').value || null)
                                 : null,
      };
    });
    jsonField.value = JSON.stringify(data);
  }

  function refreshUI() {
    const cards = [...list.querySelectorAll('.container-card')];
    cards.forEach((card, i) => card.querySelector('.card-index').textContent = i + 1);
    noMsg.style.display = cards.length === 0 ? 'block' : 'none';
    syncJson();
  }

  // ── Port rows ────────────────────────────────────────────────────────
  function addPortRow(card) {
    const portList = card.querySelector('.port-mappings-list');
    const noPortsMsg = card.querySelector('.no-ports-msg');

    const node = portTpl.content.cloneNode(true);
    const row  = node.querySelector('.port-row');
    portList.appendChild(node);

    row.querySelector('.remove-port-btn').addEventListener('click', () => {
      row.remove();
      noPortsMsg.style.display = portList.children.length === 0 ? 'block' : 'none';
      syncJson();
    });
    row.querySelectorAll('input').forEach(el => el.addEventListener('input', syncJson));

    // HTTP / TCP protocol toggle
    row.querySelectorAll('.port-proto-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        row.querySelectorAll('.port-proto-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        row.querySelector('.port-http-value').value = btn.dataset.proto === 'http' ? 'true' : 'false';
        syncJson();
      });
    });

    noPortsMsg.style.display = 'none';
    syncJson();
  }

  // ── Wire up a single container card ─────────────────────────────────
  function initCard(card, index) {
    card.dataset.index = index;

    card.querySelector('.remove-container-btn').addEventListener('click', () => {
      card.remove();
      refreshUI();
    });

    // Source tabs
    const tabs   = card.querySelectorAll('.source-tab');
    const panels = card.querySelectorAll('.source-panel');
    tabs.forEach(tab => {
      tab.addEventListener('click', () => {
        tabs.forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        const src = tab.dataset.source;
        panels.forEach(p => {
          p.style.display = p.classList.contains(`source-panel-${src}`) ? 'block' : 'none';
        });
        // Populate or refresh the registry select when switching to it
        if (src === 'registry') {
          const select = card.querySelector('.container-image-name');
          if (!registryFetched) {
            fetchRegistryImages();
          } else {
            populateSelect(select);
          }
        }
        syncJson();
      });
    });

    // Registry refresh button
    card.querySelector('.registry-refresh-btn').addEventListener('click', () => {
      registryFetched = false;
      fetchRegistryImages();
    });

    // Registry select change
    card.querySelector('.container-image-name').addEventListener('change', syncJson);

    // Label input
    card.querySelector('.container-label').addEventListener('input', syncJson);

    // Add port button
    card.querySelector('.add-port-btn').addEventListener('click', () => addPortRow(card));

    // ── TAR upload ─────────────────────────────────────────────────────
    const fileInput  = card.querySelector('.container-tar-input');
    const uploadBtn  = card.querySelector('.container-upload-btn');
    const progressWr = card.querySelector('.upload-progress-wrap');
    const progressBr = card.querySelector('.upload-progress-bar');
    const statusEl   = card.querySelector('.upload-status');
    const hiddenFn   = card.querySelector('.container-image-filename');

    const setStatus   = (msg, type) => statusEl.innerHTML = `<small class="text-${type}">${msg}</small>`;
    const setProgress = pct => { progressBr.style.width = pct + '%'; progressBr.textContent = pct + '%'; };
    const setFormLock = locked =>
      document.querySelectorAll('button[type=submit], input[type=submit]')
              .forEach(el => el.disabled = locked);

    fileInput.addEventListener('change', () => {
      const file = fileInput.files[0];
      if (!file) { uploadBtn.disabled = true; return; }
      if (!file.name.toLowerCase().endsWith('.tar')) {
        setStatus('Please select a .tar file.', 'danger');
        fileInput.value = '';
        uploadBtn.disabled = true;
        return;
      }
      hiddenFn.value = '';
      statusEl.innerHTML = '';
      progressWr.style.display = 'none';
      setProgress(0);
      uploadBtn.disabled = false;
      syncJson();
    });

    uploadBtn.addEventListener('click', () => {
      const file = fileInput.files[0];
      if (!file) return;

      const formData = new FormData();
      formData.append('image_tar', file);
      formData.append('nonce', init.csrfNonce);

      const xhr = new XMLHttpRequest();

      xhr.upload.addEventListener('loadstart', () => {
        progressWr.style.display = 'block';
        uploadBtn.disabled = fileInput.disabled = true;
        setFormLock(true);
        setStatus('Uploading…', 'info');
        setProgress(0);
      });

      xhr.upload.addEventListener('progress', e => {
        if (e.lengthComputable) setProgress(Math.round(e.loaded / e.total * 100));
      });

      xhr.addEventListener('load', () => {
        fileInput.disabled = false;
        setFormLock(false);
        let data;
        try { data = JSON.parse(xhr.responseText); }
        catch (_) {
          setStatus(`Upload failed: unexpected server response (HTTP ${xhr.status}).`, 'danger');
          fileInput.value = '';
          uploadBtn.disabled = true;
          return;
        }
        if (data.success) {
          hiddenFn.value = data.filename;
          setProgress(100);
          progressBr.classList.remove('progress-bar-animated');
          setStatus('✓ ' + data.filename, 'success');
          uploadBtn.disabled = true;
          // Offer this image in the registry dropdown next time
          registryFetched = false;
          syncJson();
        } else {
          setStatus('Upload failed: ' + (data.error || 'unknown error'), 'danger');
          progressWr.style.display = 'none';
          fileInput.value = '';
          uploadBtn.disabled = true;
        }
      });

      xhr.addEventListener('error', () => {
        fileInput.disabled = false;
        setFormLock(false);
        setStatus('Upload failed: network error.', 'danger');
        progressWr.style.display = 'none';
        fileInput.value = '';
        uploadBtn.disabled = true;
      });

      xhr.addEventListener('abort', () => {
        fileInput.disabled = false;
        setFormLock(false);
        setStatus('Upload cancelled.', 'danger');
        progressWr.style.display = 'none';
        uploadBtn.disabled = false;
      });

      xhr.open('POST', uploadUrl);
      xhr.setRequestHeader('CSRF-Token', init.csrfNonce);
      xhr.send(formData);
    });
  }

  // ── Add container ────────────────────────────────────────────────────
  addBtn.addEventListener('click', () => {
    const index = containerSeq++;
    const node  = cardTpl.content.cloneNode(true);
    const card  = node.querySelector('.container-card');
    list.appendChild(node);
    initCard(card, index);
    refreshUI();
  });

  // Pre-fetch image list so the dropdown is ready when the tab is clicked.
  fetchRegistryImages();
  refreshUI();
})();
