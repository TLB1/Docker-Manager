(function () {
  const meta         = document.getElementById("page-meta");
  const TOKEN        = meta.dataset.token;
  const CTFD_ROOT    = meta.dataset.ctfdRoot;
  const CTFD_DOMAIN  = meta.dataset.ctfdDomain;
  const CSRF         = meta.dataset.csrf;
  const STATUS_URL   = CTFD_ROOT + "/docker/api/token/" + TOKEN + "/status";
  const RESUME_URL   = CTFD_ROOT + "/docker/api/token/" + TOKEN + "/resume";
  const CHALLENGE_URL = "http://" + TOKEN + "." + CTFD_DOMAIN + ":8008/";

  const icon         = document.getElementById("icon");
  const heading      = document.getElementById("heading");
  const subtitle     = document.getElementById("subtitle");
  const badgeWrap    = document.getElementById("status-badge-wrap");
  const actionWrap   = document.getElementById("action-wrap");
  const progressWrap = document.getElementById("progress-wrap");
  const progressBar  = document.getElementById("progress-bar");
  const msg          = document.getElementById("msg");

  function setMsg(text, isError) {
    msg.textContent = text;
    msg.className = "small mt-2 " + (isError ? "text-danger" : "text-secondary");
  }

  function setBadge(status) {
    const colour = status === "running" ? "success"
                 : (status === "stopped" || status === "exited") ? "warning"
                 : "secondary";
    badgeWrap.innerHTML =
      `<span class="badge rounded-pill bg-${colour} text-uppercase">${status}</span>`;
  }

  function renderRunning() {
    icon.textContent = "🔴";
    heading.textContent = "Something Went Wrong";
    subtitle.textContent = "Your container is running but the connection failed. Try reloading it.";
    setBadge("running");
    actionWrap.innerHTML =
      `<a class="btn btn-primary" href="${CHALLENGE_URL}">Try Again</a>`;
  }

  function renderSuspended(status) {
    icon.textContent = "💤";
    heading.textContent = "Container Suspended";
    subtitle.textContent = "Your container was suspended due to inactivity. Resume it to continue.";
    setBadge(status);
    actionWrap.innerHTML =
      `<button class="btn btn-warning" id="resume-btn">Resume Container</button>`;
    document.getElementById("resume-btn").addEventListener("click", doResume);
  }

  function renderGo() {
    icon.textContent = "✅";
    heading.textContent = "Container Ready";
    subtitle.textContent = "Your container is running.";
    setBadge("running");
    actionWrap.innerHTML =
      `<a class="btn btn-success" href="${CHALLENGE_URL}">Go to Challenge</a>`;
    setMsg("");
    progressWrap.style.setProperty("display", "none", "important");
  }

  function renderNotFound() {
    icon.textContent = "❌";
    heading.textContent = "Container Not Found";
    subtitle.textContent = "This container no longer exists. Return to the challenges page to start a new one.";
    badgeWrap.innerHTML = "";
    actionWrap.innerHTML =
      `<a class="btn btn-primary" href="${CTFD_ROOT}/challenges">Back to Challenges</a>`;
  }

  function pollUntilRunning(attemptsLeft) {
    if (attemptsLeft <= 0) {
      setMsg("Container is taking a while — try refreshing.", true);
      progressWrap.style.setProperty("display", "none", "important");
      return;
    }
    setTimeout(() => {
      fetch(STATUS_URL)
        .then(r => r.json())
        .then(data => {
          if (!data.success) { setMsg(data.error || "Status check failed", true); return; }
          if (!data.exists)  { renderNotFound(); return; }
          if (data.status === "running") {
            progressBar.style.width = "100%";
            setTimeout(renderGo, 300);
          } else {
            progressBar.style.width = Math.round(((10 - attemptsLeft) / 10) * 90) + "%";
            pollUntilRunning(attemptsLeft - 1);
          }
        })
        .catch(() => pollUntilRunning(attemptsLeft - 1));
    }, 2000);
  }

  function doResume() {
    const btn = document.getElementById("resume-btn");
    if (btn) btn.disabled = true;
    setMsg("Resuming container…");
    progressWrap.style.setProperty("display", "flex", "important");
    progressBar.style.width = "10%";

    fetch(RESUME_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "CSRF-Token": CSRF
      },
    })
      .then(r => r.json())
      .then(data => {
        if (!data.success) {
          setMsg(data.error || "Failed to resume", true);
          progressWrap.style.setProperty("display", "none", "important");
          if (btn) btn.disabled = false;
          return;
        }
        setMsg("Resumed! Waiting for container…");
        progressBar.style.width = "20%";
        pollUntilRunning(10);
      })
      .catch(err => {
        setMsg("Resume failed: " + err.message, true);
        progressWrap.style.setProperty("display", "none", "important");
        if (btn) btn.disabled = false;
      });
  }

  // Initial status check on page load
  fetch(STATUS_URL)
    .then(r => r.json())
    .then(data => {
      if (!data.success) { setMsg(data.error || "Could not fetch status", true); return; }
      if (!data.exists)  { renderNotFound(); return; }
      data.status === "running" ? renderRunning() : renderSuspended(data.status);
    })
    .catch(() => setMsg("Could not reach CTFd", true));
})();
