(function () {
  "use strict";

  const $ = CTFd.lib.$;

  CTFd._internal.challenge.preRender = function () {};
  CTFd._internal.challenge.postRender = function () {};

  /* ---------------- API helpers ---------------- */
    
  function apiGet(path) {
    if (CTFd.api && CTFd.api.get) {
      return CTFd.api.get(path);
    }
    return fetch(path, { credentials: "same-origin" }).then(r => r.json());
  }

    function csrf() {
      return CTFd.config.csrfNonce || "";
    }

    function apiPost(path, data) {
    return fetch(path, {
        method: "POST",
        credentials: "same-origin",   // session cookie
        headers: {
        "Content-Type": "application/json",
        "CSRF-Token": csrf()
        },
        body: JSON.stringify(data)
    }).then(r => r.json());
    }

  /* ---------------- UI rendering ---------------- */

  function renderControls($container, resp) {
    if (!$container || !$container.length) return;

    if (!resp || !resp.exists) {
      $container.html(
        '<button id="docker-start" class="btn btn-primary">Start container</button>' +
        ' <span id="docker-msg" class="ml-2"></span>'
      );
      return;
    }

    if (resp.status === "running") {
      $container.html(
        `<a id="docker-go" class="btn btn-success" href="${resp.url}" target="_blank">Go to challenge</a>` +
        ' <span id="docker-msg" class="ml-2"></span>'
      );
      return;
    }

    $container.html(
      `<button id="docker-resume" class="btn btn-warning">Resume container</button>` +
      ` <span class="ml-2 text-muted">Status: ${resp.status}</span>` +
      ' <span id="docker-msg" class="ml-2"></span>'
    );
  }

  /* ---------------- Docker logic ---------------- */

  function docker_update_ui(challenge_id) {
    const $controls = $("#docker-controls");
    if (!$controls.length) return;

    apiGet(`/docker/api/challenge/${challenge_id}/status`)
      .then(function (resp) {
        if (!resp || !resp.success) {
          $controls.find("#docker-msg").text(resp?.error || "Cannot get docker status");
          return;
        }
        renderControls($controls, resp);
      })
      .catch(function () {
        $controls.find("#docker-msg").text("Error checking container");
      });
  }

function docker_start(challenge_id) {
    const $controls = $("#docker-controls");
    $controls.find("#docker-msg").text("Starting container…");
    $controls.find("#docker-start").prop("disabled", true);

    apiPost(`/docker/api/challenge/${challenge_id}/start`, {})
        .then(function (resp) {
            if (resp.success) {
                $controls.find("#docker-msg").text("Started! Waiting for container…");
                pollUntilRunning(challenge_id, 10); // retry up to 10 times
            } else {
                $controls.find("#docker-msg").text(resp.error || "Failed to start");
                $controls.find("#docker-start").prop("disabled", false);
            }
        })
        .catch(err => {
            console.error("[docker] start error:", err);
            $controls.find("#docker-msg").text("Failed to start: " + err.message);
            $controls.find("#docker-start").prop("disabled", false);
        });
}

function pollUntilRunning(challenge_id, attemptsLeft) {
    if (attemptsLeft <= 0) {
        $("#docker-controls").find("#docker-msg").text("Container is taking a while — try refreshing.");
        return;
    }
    setTimeout(() => {
        apiGet(`/docker/api/challenge/${challenge_id}/status`)
            .then(resp => {
                if (resp.success && resp.exists && resp.status === "running") {
                    renderControls($("#docker-controls"), resp);
                } else {
                    pollUntilRunning(challenge_id, attemptsLeft - 1);
                }
            })
            .catch(() => pollUntilRunning(challenge_id, attemptsLeft - 1));
    }, 2000); // check every 2s
}

  function docker_resume(challenge_id) {
    const $controls = $("#docker-controls");
    $controls.find("#docker-msg").text("Resuming container…");

    apiPost(`/docker/api/challenge/${challenge_id}/resume`, {})
      .then(function (resp) {
        if (resp.success) {
          $controls.find("#docker-msg").text("Resumed. Refreshing…");
          setTimeout(() => docker_update_ui(challenge_id), 1000);
        } else {
          $controls.find("#docker-msg").text(resp.error || "Failed to resume");
        }
      })
      .catch(() => $controls.find("#docker-msg").text("Failed to resume"));
  }

  /* ---------------- Event delegation ---------------- */

  $(document).on("click", "#docker-start", function () {
    const challenge_id = parseInt($("#docker-controls").attr("data-challenge-id"));
    docker_start(challenge_id);
  });

  $(document).on("click", "#docker-resume", function () {
    const challenge_id = parseInt($("#docker-controls").attr("data-challenge-id"));
    docker_resume(challenge_id);
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