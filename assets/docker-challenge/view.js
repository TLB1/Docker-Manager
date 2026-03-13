(function () {
  "use strict";

  const $ = CTFd.lib.$;

  CTFd._internal.challenge.preRender = function () {};
  CTFd._internal.challenge.postRender = function () {};

  /* ---------------- API helpers ---------------- */

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
      if (!r.ok && r.headers.get("content-type")?.includes("text/html")) {
        throw new Error(`HTTP ${r.status}`);
      }
      return r.json();
    });
  }

  /* ---------------- UI rendering ---------------- */

  function renderControls($container, resp) {
    if (!$container || !$container.length) return;

    if (!resp || !resp.exists) {
      $container.html(
        `<button id="docker-start" class="btn btn-primary">Start container</button>` +
        ` <span id="docker-msg" class="ms-2"></span>`
      );
      return;
    }

    if (resp.status === "running") {
      $container.html(
        `<a id="docker-go" class="btn btn-success" href="${resp.url}" target="_blank">Go to challenge</a>` +
        ` <button id="docker-stop"  class="btn btn-secondary ms-1">Stop</button>` +
        ` <button id="docker-reset" class="btn btn-danger ms-1">Reset</button>` +
        ` <span id="docker-msg" class="ms-2"></span>`
      );
      return;
    }

    // stopped / exited / other
    $container.html(
      `<button id="docker-resume" class="btn btn-warning">Resume</button>` +
      ` <button id="docker-reset" class="btn btn-danger ms-1">Reset</button>` +
      ` <span class="ms-2 text-muted">Status: ${resp.status}</span>` +
      ` <span id="docker-msg" class="ms-2"></span>`
    );
  }

  /* ---------------- Helpers ---------------- */

  function setMsg(text, isError) {
    const $msg = $("#docker-msg");
    $msg.text(text);
    $msg.removeClass("text-danger text-muted");
    $msg.addClass(isError ? "text-danger" : "text-muted");
  }

  function setAllDisabled(disabled) {
    $("#docker-controls").find("button, a.btn").each(function () {
      $(this).prop("disabled", disabled);
    });
  }

  /* ---------------- Docker logic ---------------- */

  function docker_update_ui(challenge_id) {
    const $controls = $("#docker-controls");
    if (!$controls.length) return;

    apiGet(`/docker/api/challenge/${challenge_id}/status`)
      .then(resp => {
        if (!resp || !resp.success) {
          setMsg(resp?.error || "Cannot get docker status", true);
          return;
        }
        renderControls($controls, resp);
      })
      .catch(() => setMsg("Error checking container status", true));
  }

  function docker_start(challenge_id) {
    setMsg("Starting container…");
    setAllDisabled(true);

    apiPost(`/docker/api/challenge/${challenge_id}/start`, {})
      .then(resp => {
        if (resp.success) {
          setMsg("Started! Waiting for container…");
          pollUntilRunning(challenge_id, 10);
        } else {
          setMsg(resp.error || "Failed to start", true);
          setAllDisabled(false);
        }
      })
      .catch(err => {
        setMsg("Failed to start: " + err.message, true);
        setAllDisabled(false);
      });
  }

  function docker_resume(challenge_id) {
    setMsg("Resuming container…");
    setAllDisabled(true);

    apiPost(`/docker/api/challenge/${challenge_id}/resume`, {})
      .then(resp => {
        if (resp.success) {
          setMsg("Resumed! Waiting for container…");
          pollUntilRunning(challenge_id, 10);
        } else {
          setMsg(resp.error || "Failed to resume", true);
          setAllDisabled(false);
        }
      })
      .catch(err => {
        setMsg("Failed to resume: " + err.message, true);
        setAllDisabled(false);
      });
  }

  function docker_stop(challenge_id) {
    if (!confirm("Stop and delete your container? Your progress will be lost.")) return;
    setMsg("Stopping container…");
    setAllDisabled(true);

    apiPost(`/docker/api/challenge/${challenge_id}/stop`, {})
      .then(resp => {
        if (resp.success) {
          setMsg("Container stopped.");
          docker_update_ui(challenge_id);
        } else {
          setMsg(resp.error || "Failed to stop", true);
          setAllDisabled(false);
        }
      })
      .catch(err => {
        setMsg("Failed to stop: " + err.message, true);
        setAllDisabled(false);
      });
  }

  function docker_reset(challenge_id) {
    if (!confirm("Reset your container? This will delete it and start a fresh one.")) return;
    setMsg("Resetting container…");
    setAllDisabled(true);

    apiPost(`/docker/api/challenge/${challenge_id}/reset`, {})
      .then(resp => {
        if (resp.success) {
          setMsg("Reset! Waiting for new container…");
          pollUntilRunning(challenge_id, 10);
        } else {
          setMsg(resp.error || "Failed to reset", true);
          setAllDisabled(false);
        }
      })
      .catch(err => {
        setMsg("Failed to reset: " + err.message, true);
        setAllDisabled(false);
      });
  }

  function pollUntilRunning(challenge_id, attemptsLeft) {
    if (attemptsLeft <= 0) {
      setMsg("Container is taking a while — try refreshing.", true);
      return;
    }
    setTimeout(() => {
      apiGet(`/docker/api/challenge/${challenge_id}/status`)
        .then(resp => {
          if (resp.success && resp.exists && resp.status === "running") {
            renderControls($("#docker-controls"), resp);
            setMsg("");
          } else {
            pollUntilRunning(challenge_id, attemptsLeft - 1);
          }
        })
        .catch(() => pollUntilRunning(challenge_id, attemptsLeft - 1));
    }, 2000);
  }

  /* ---------------- Event delegation ---------------- */

  $(document).on("click", "#docker-start",  function () {
    docker_start(parseInt($("#docker-controls").attr("data-challenge-id")));
  });
  $(document).on("click", "#docker-resume", function () {
    docker_resume(parseInt($("#docker-controls").attr("data-challenge-id")));
  });
  $(document).on("click", "#docker-stop",   function () {
    docker_stop(parseInt($("#docker-controls").attr("data-challenge-id")));
  });
  $(document).on("click", "#docker-reset",  function () {
    docker_reset(parseInt($("#docker-controls").attr("data-challenge-id")));
  });

  /* ---------------- Init ---------------- */

  $(function () {
    const challenge_id =
      parseInt($("#challenge-id").val()) ||
      parseInt($("#docker-controls").attr("data-challenge-id"));
    if (!challenge_id) return;
    docker_update_ui(challenge_id);
  });

})();