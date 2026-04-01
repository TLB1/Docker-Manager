const _meta        = document.getElementById("nodes-meta");
const CSRF         = _meta.dataset.csrf;
const _suspendUrl  = _meta.dataset.suspendUrl;
const _resumeUrl   = _meta.dataset.resumeUrl;

async function deleteContainer(token, btn) {
  if (!confirm("Delete this container? This cannot be undone.")) return;

  const resp = await fetch("/admin/container/delete", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "CSRF-Token": CSRF
    },
    body: JSON.stringify({ token })
  });

  if (resp.ok) {
    const card = btn.closest(".card");
    if (card) card.remove();
  } else {
    alert("Failed to delete container");
  }
}

async function suspendContainer(token, btn) {
  if (!confirm("Suspend this container?")) return;

  const resp = await fetch(_suspendUrl, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "CSRF-Token": CSRF
    },
    body: JSON.stringify({ token })
  });

  if (resp.ok) btn.closest(".card").style.opacity = 0.5;
  else alert("Suspend failed");
}

async function resumeContainer(token, btn) {
  const resp = await fetch(_resumeUrl, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "CSRF-Token": CSRF
    },
    body: JSON.stringify({ token })
  });

  if (resp.ok) btn.closest(".card").style.opacity = 0.5;
  else alert("Resume failed");
}

const sortSelect  = document.getElementById("containerSort");
const searchInput = document.getElementById("containerSearch");
const list        = document.getElementById("containersList");

function updateList() {
  const cards = Array.from(list.querySelectorAll(".container-card"));
  const mode = sortSelect.value;
  const search = searchInput.value.trim().toLowerCase();

  // Sort
  cards.sort((a, b) => {
    if (mode === "running") {
      return (b.dataset.status === "running") - (a.dataset.status === "running");
    }
    if (mode === "node") {
      return a.dataset.node.localeCompare(b.dataset.node);
    }
    if (mode === "challenge") {
      return a.dataset.challenge.localeCompare(b.dataset.challenge);
    }
  });

  // Filter
  cards.forEach(c => {
    const team      = c.dataset.team.toLowerCase();
    const challenge = c.dataset.challenge.toLowerCase();
    const image     = c.dataset.image.toLowerCase();

    if (
      search === "" ||
      team.includes(search) ||
      challenge.includes(search) ||
      image.includes(search)
    ) {
      c.style.display = "";
    } else {
      c.style.display = "none";
    }

    list.appendChild(c);
  });
}

// Event listeners
sortSelect.addEventListener("change", updateList);
searchInput.addEventListener("input", updateList);

// Trigger on page load
document.addEventListener("DOMContentLoaded", updateList);
