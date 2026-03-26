(function () {
  "use strict";

  const $ = CTFd.lib.$;

  CTFd._internal.challenge.preRender  = function () {};
  CTFd._internal.challenge.postRender = function () {};

  // Namespace all delegated events so we can cleanly remove them before
  // re-attaching. CTFd re-executes this script each time the modal opens,
  // which would otherwise stack duplicate listeners.
  const NS = ".dockerChallenge";

  /* ─────────────────────────────────────────────────────────────────────
   * API helpers
   * ───────────────────────────────────────────────────────────────────── */

  function apiGet(path) {
    return fetch(path, { credentials: "same-origin" }).then(r => r.json());
  }

  function apiPost(path, data) {
    return fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "CSRF-Token": init.csrfNonce,
      },
      body: JSON.stringify(data),
    }).then(r => {
      if (!r.ok && r.headers.get("content-type")?.includes("text/html"))
        throw new Error(`HTTP ${r.status}`);
      return r.json();
    });
  }

  /* ─────────────────────────────────────────────────────────────────────
   * Rendering
   * ───────────────────────────────────────────────────────────────────── */

  function renderActionBar(containers) {
    const $bar    = $("#docker-action-bar");
    const exists  = containers.some(c => c.exists);
    const someStop = exists && containers.some(c => c.exists && c.status !== "running");

    let html = "";
    if (!exists) {
      html += `<button id="docker-start" class="btn btn-primary">Start</button>`;
    } else {
      if (someStop)
        html += `<button id="docker-resume" class="btn btn-warning me-1">Resume</button>`;
      html += `<button id="docker-stop"  class="btn btn-secondary me-1">Stop</button>`;
      html += `<button id="docker-reset" class="btn btn-danger">Reset</button>`;
    }
    html += `<span id="docker-msg" class="ms-2 small"></span>`;
    $bar.html(html);
  }

  function renderCards(containers) {
    const $grid = $("#docker-container-cards");
    if (!$grid.length) return;

    $grid.html(
      containers.map(c => {
        const running = c.exists && c.status === "running";
        const stopped = c.exists && !running;

        const borderClass = running ? "border-success" : stopped ? "border-warning" : "border-secondary";
        const badgeClass  = running ? "bg-success"     : stopped ? "bg-warning text-dark" : "bg-secondary";
        const statusText  = c.exists ? c.status : "not started";

        let bodyHtml;
        if (running) {
          const mappings = c.port_mappings || [];
          if (mappings.length) {
            bodyHtml = mappings.map(pm => {
              const label = escHtml(pm.label || "Port " + pm.container_port);
              if (pm.http === false || pm.http === "false") {
                // TCP port — show copyable address, not a link
                const domain = window.location.hostname.split('.').slice(-2).join('.') || window.location.hostname;
                const addr   = pm.ctfd_tcp_port
                  ? escHtml(`${domain}:${pm.ctfd_tcp_port}`)
                  : escHtml(`(allocating…)`);
                return `
                  <span class="btn btn-sm btn-success me-1 mb-1 font-monospace"
                        style="cursor:pointer; user-select:all;"
                        title="TCP — click to copy"
                        onclick="navigator.clipboard.writeText('${addr}')">
                    ${label}: ${addr}
                  </span>`;
              }
              // HTTP port — clickable link
              return `<a href="${c.url}" target="_blank" class="btn btn-sm btn-success me-1 mb-1">
                ${label} ↗
              </a>`;
            }).join("");
          } else {
            bodyHtml = `<a href="${c.url}" target="_blank" class="btn btn-sm btn-success">Open ↗</a>`;
          }
        } else if (stopped) {
          bodyHtml = `<span class="text-muted small">Status: ${escHtml(c.status)}</span>`;
        } else {
          const ports = (c.port_mappings || []).map(p => p.container_port).filter(Boolean);
          bodyHtml = `<span class="text-muted small">${ports.length ? "Ports: " + ports.join(", ") : "Not running"}</span>`;
        }

        return `
          <div class="col-12 col-md-12">
            <div class="card h-100 ${borderClass}">
              <div class="card-header d-flex align-items-center justify-content-between py-2">
                <span class="fw-semibold">${escHtml(c.label)}</span>
                <span class="badge ${badgeClass}">${escHtml(statusText)}</span>
              </div>
              <div class="card-body py-2">${bodyHtml}</div>
            </div>
          </div>`;
      }).join("")
    );
  }

  function renderAll(containers) {
    renderActionBar(containers);
    renderCards(containers);

    const exists  = containers.some(c => c.exists);
    const allRun  = exists && containers.every(c => !c.exists || c.status === "running");
    const someSus = exists && containers.some(c => c.exists && c.status !== "running");

    if (!exists)       setMsg("Container(s) not running");
    else if (allRun)   setMsg("Container(s) are running");
    else if (someSus)  setMsg("Container(s) are suspended");
  }

  function escHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  /* ─────────────────────────────────────────────────────────────────────
   * Message / lock helpers — read from live DOM, not stale refs
   * ───────────────────────────────────────────────────────────────────── */

  function setMsg(text, isError = false) {
    $("#docker-msg")
      .text(text)
      .removeClass("text-danger text-muted")
      .addClass(isError ? "text-danger" : "text-muted");
  }

  function setAllDisabled(disabled) {
    $("#docker-action-bar").find("button").prop("disabled", disabled);
  }

  /* ─────────────────────────────────────────────────────────────────────
   * Status fetch
   * ───────────────────────────────────────────────────────────────────── */

  function docker_update_ui(challenge_id) {
    apiGet(`/docker/api/challenge/${challenge_id}/status`)
      .then(resp => {
        if (!resp?.success) { setMsg(resp?.error || "Cannot get status", true); return; }
        renderAll(resp.containers || []);
      })
      .catch(() => setMsg("Error checking container status", true));
  }

  /* ─────────────────────────────────────────────────────────────────────
   * Actions
   * ───────────────────────────────────────────────────────────────────── */

  function docker_start(challenge_id) {
    setMsg("Starting containers…");
    setAllDisabled(true);
    apiPost(`/docker/api/challenge/${challenge_id}/start`, {})
      .then(resp => {
        if (resp.success) { setMsg("Starting…"); pollUntilAllRunning(challenge_id, 15); }
        else { setMsg(resp.error || "Failed to start", true); setAllDisabled(false); }
      })
      .catch(err => { setMsg("Failed to start: " + err.message, true); setAllDisabled(false); });
  }

  function docker_resume(challenge_id) {
    setMsg("Resuming containers…");
    setAllDisabled(true);
    apiPost(`/docker/api/challenge/${challenge_id}/resume`, {})
      .then(resp => {
        if (resp.success) { setMsg("Resuming…"); pollUntilAllRunning(challenge_id, 15); }
        else { setMsg(resp.error || "Failed to resume", true); setAllDisabled(false); }
      })
      .catch(err => { setMsg("Failed to resume: " + err.message, true); setAllDisabled(false); });
  }

  function docker_stop(challenge_id) {
    if (!confirm("Stop and delete all containers for this challenge? Your progress will be lost.")) return;
    setMsg("Stopping…");
    setAllDisabled(true);
    apiPost(`/docker/api/challenge/${challenge_id}/stop`, {})
      .then(resp => {
        if (resp.success) { docker_update_ui(challenge_id); setMsg("Containers stopped."); }
        else { setMsg(resp.error || "Failed to stop", true); setAllDisabled(false); }
      })
      .catch(err => { setMsg("Failed to stop: " + err.message, true); setAllDisabled(false); });
  }

  function docker_reset(challenge_id) {
    if (!confirm("Reset all containers? This will delete them and start fresh ones.")) return;
    setMsg("Resetting…");
    setAllDisabled(true);
    apiPost(`/docker/api/challenge/${challenge_id}/reset`, {})
      .then(resp => {
        if (resp.success) { setMsg("Resetting…"); pollUntilAllRunning(challenge_id, 15); }
        else { setMsg(resp.error || "Failed to reset", true); setAllDisabled(false); }
      })
      .catch(err => { setMsg("Failed to reset: " + err.message, true); setAllDisabled(false); });
  }

  /* ─────────────────────────────────────────────────────────────────────
   * Polling
   * ───────────────────────────────────────────────────────────────────── */

  function pollUntilAllRunning(challenge_id, attemptsLeft) {
    if (attemptsLeft <= 0) {
      setMsg("Containers are taking a while — try refreshing.", true);
      setAllDisabled(false);
      return;
    }
    setTimeout(() => {
      apiGet(`/docker/api/challenge/${challenge_id}/status`)
        .then(resp => {
          if (!resp?.success) { pollUntilAllRunning(challenge_id, attemptsLeft - 1); return; }

          const containers = resp.containers || [];
          renderAll(containers);

          const allRunning = containers.length > 0 &&
                             containers.every(c => c.exists && c.status === "running");
          if (allRunning) {
            // renderAll() will set the correct status message
          } else {
            const n = containers.filter(c => c.status === "running").length;
            setMsg(`${n}/${containers.length} running…`);
            pollUntilAllRunning(challenge_id, attemptsLeft - 1);
          }
        })
        .catch(() => pollUntilAllRunning(challenge_id, attemptsLeft - 1));
    }, 2000);
  }

  /* ─────────────────────────────────────────────────────────────────────
   * Event binding — unbind namespace first, then re-attach once
   * ───────────────────────────────────────────────────────────────────── */

  function getChallengeId() {
    return parseInt(
      $("#challenge-id").val() ||
      $("#docker-controls").attr("data-challenge-id")
    );
  }

  function bindEvents() {
    // Remove any handlers registered by a previous execution of this script
    $(document).off(NS);

    $(document).on("click" + NS, "#docker-start",  () => docker_start(getChallengeId()));
    $(document).on("click" + NS, "#docker-resume", () => docker_resume(getChallengeId()));
    $(document).on("click" + NS, "#docker-stop",   () => docker_stop(getChallengeId()));
    $(document).on("click" + NS, "#docker-reset",  () => docker_reset(getChallengeId()));
  }

  /* ─────────────────────────────────────────────────────────────────────
   * Init
   * ───────────────────────────────────────────────────────────────────── */

  $(function () {
    const challenge_id = getChallengeId();
    if (!challenge_id) return;
    bindEvents();
    docker_update_ui(challenge_id);
  });

})();