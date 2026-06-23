const API = "";
const sseConnections = {};

// ─── State ──────────────────────────────────────────────────────────────────

let downloadTasks = {};

// ─── DOM Refs ───────────────────────────────────────────────────────────────

const urlInput = document.getElementById("url-input");
const downloadBtn = document.getElementById("download-btn");
const urlTypeBadge = document.getElementById("url-type-badge");
const downloadList = document.getElementById("download-list");
const queueEmpty = document.getElementById("queue-empty");
const queueBadge = document.getElementById("queue-badge");

// ─── URL Validation ─────────────────────────────────────────────────────────

const SPOTIFY_REGEX = /https?:\/\/open\.spotify\.com\/(track|playlist|album|artist)\/[a-zA-Z0-9]+/;
const YT_REGEX = /https?:\/\/(www\.)?(youtube\.com\/watch\?v=|youtu\.be\/|music\.youtube\.com\/watch\?v=|youtube\.com\/playlist\?list=|music\.youtube\.com\/playlist\?list=)[a-zA-Z0-9_-]+/;

const TYPE_LABELS = {
  track: "🎵 Música",
  playlist: "📋 Playlist/Album",
  album: "💿 Álbum",
  artist: "🎤 Artista",
};

const TYPE_ICONS = {
  track: "🎵",
  playlist: "📋",
  album: "💿",
  artist: "🎤",
};

function detectUrlType(url) {
  if (url.includes("playlist") || url.includes("album") || url.includes("artist") || url.includes("list=")) {
    return "playlist";
  }
  if (SPOTIFY_REGEX.test(url) || YT_REGEX.test(url)) {
    return "track";
  }
  return null;
}

// URL input listener
urlInput.addEventListener("input", () => {
  const url = urlInput.value.trim();
  const type = detectUrlType(url);
  if (type) {
    urlTypeBadge.textContent = TYPE_LABELS[type];
    urlTypeBadge.classList.add("visible");
  } else {
    urlTypeBadge.classList.remove("visible");
  }
});

// Enter key to submit
urlInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    handleDownload();
  }
});

// ─── Download Handler ───────────────────────────────────────────────────────

async function handleDownload() {
  const url = urlInput.value.trim();

  if (!url) {
    showToast("Cole uma URL do Spotify ou YouTube para começar", "error");
    urlInput.focus();
    return;
  }

  if (!SPOTIFY_REGEX.test(url) && !YT_REGEX.test(url)) {
    showToast("URL inválida. Use um link do Spotify ou YouTube.", "error");
    return;
  }

  downloadBtn.classList.add("loading");
  downloadBtn.disabled = true;

  try {
    const res = await fetch(`${API}/api/download`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });

    const data = await res.json();

    if (!res.ok) {
      showToast(data.error || "Erro ao iniciar download", "error");
      return;
    }

    const type = detectUrlType(url);
    showToast(`Download iniciado — ${TYPE_LABELS[type] || "Música"}`, "success");

    urlInput.value = "";
    urlTypeBadge.classList.remove("visible");

    connectSSE(data.task_id);
    await refreshQueue();
  } catch (err) {
    showToast("Erro de conexão com o servidor", "error");
    console.error(err);
  } finally {
    downloadBtn.classList.remove("loading");
    downloadBtn.disabled = false;
  }
}

// ─── Trigger file download in browser ───────────────────────────────────────

function triggerDownload(url, filename) {
  const a = document.createElement("a");
  a.href = `${API}${url}`;
  a.download = filename || "";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

// ─── SSE Progress ───────────────────────────────────────────────────────────

function connectSSE(taskId) {
  if (sseConnections[taskId]) return;

  const evtSource = new EventSource(`${API}/api/progress/${taskId}`);
  sseConnections[taskId] = evtSource;

  evtSource.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      downloadTasks[taskId] = data;
      renderQueue();

      if (data.status === "completed") {
        evtSource.close();
        delete sseConnections[taskId];

        // Auto-download: ZIP for playlists, file for single tracks
        if (data.zip_url) {
          showToast("Playlist completa! Baixando ZIP... 📦", "success");
          triggerDownload(data.zip_url, "");
        } else if (data.file_url) {
          showToast("Download concluído! ✅", "success");
          triggerDownload(data.file_url, "");
        } else {
          showToast("Download concluído! ✅", "success");
        }
      } else if (data.status === "error") {
        evtSource.close();
        delete sseConnections[taskId];
        const errorMsg = data.errors?.length
          ? data.errors[data.errors.length - 1]
          : "Erro durante o download";
        showToast(errorMsg, "error");
      } else if (data.status === "cancelled") {
        evtSource.close();
        delete sseConnections[taskId];
      }
    } catch (e) {
      console.error("SSE parse error:", e);
    }
  };

  evtSource.onerror = () => {
    evtSource.close();
    delete sseConnections[taskId];
  };
}

// ─── Queue Management ───────────────────────────────────────────────────────

async function refreshQueue() {
  try {
    const res = await fetch(`${API}/api/queue`);
    const tasks = await res.json();

    for (const task of tasks) {
      downloadTasks[task.id] = task;
      if (
        (task.status === "downloading" || task.status === "queued") &&
        !sseConnections[task.id]
      ) {
        connectSSE(task.id);
      }
    }

    renderQueue();
  } catch (err) {
    console.error("Error refreshing queue:", err);
  }
}

async function cancelDownload(taskId) {
  try {
    await fetch(`${API}/api/queue/${taskId}`, { method: "DELETE" });
    if (downloadTasks[taskId]) {
      downloadTasks[taskId].status = "cancelled";
    }
    renderQueue();
    showToast("Download cancelado", "error");
  } catch (err) {
    console.error("Error cancelling:", err);
  }
}

function renderQueue() {
  const tasks = Object.values(downloadTasks).sort(
    (a, b) => (b.created_at || "").localeCompare(a.created_at || "")
  );

  const activeCount = tasks.filter(
    (t) => t.status === "downloading" || t.status === "queued"
  ).length;
  queueBadge.textContent = activeCount > 0 ? activeCount : "";

  if (tasks.length === 0) {
    queueEmpty.style.display = "";
    Array.from(downloadList.children).forEach((child) => {
      if (child.id !== "queue-empty") child.remove();
    });
    return;
  }

  queueEmpty.style.display = "none";

  const existingIds = new Set();
  tasks.forEach((task, idx) => {
    existingIds.add(`card-${task.id}`);
    let card = document.getElementById(`card-${task.id}`);

    if (!card) {
      card = document.createElement("div");
      card.className = "download-card";
      card.id = `card-${task.id}`;
      card.style.animationDelay = `${idx * 50}ms`;
      downloadList.appendChild(card);
    }

    const typeIcon = TYPE_ICONS[task.type] || "🎵";
    const typeName = (task.type || "track").toUpperCase();
    const showCancel = task.status === "downloading" || task.status === "queued";
    const showProgress = task.status === "downloading" || task.status === "queued";
    const trackInfo = task.current_track
      ? `<div class="card-track">♪ ${escapeHtml(task.current_track)}</div>`
      : task.status === "queued" || task.status === "downloading"
        ? `<div class="card-track card-track-pending">♪ Aguardando...</div>`
        : "";
    const progressLabel =
      task.total_tracks > 1
        ? `${task.completed_tracks || 0} / ${task.total_tracks} faixas`
        : task.status === "downloading" && (task.progress || 0) === 0
          ? "Pesquisando..."
          : "";
    const errorsHtml =
      task.errors && task.errors.length > 0
        ? `<div class="card-errors">${task.errors.map(escapeHtml).join("<br>")}</div>`
        : "";

    const tracksHtml = (task.tracks && task.tracks.length >= 1)
      ? `<div class="card-tracklist">${task.tracks.map(t => {
          const icons = { pending: "⏳", downloading: "▶", completed: "✅", error: "❌" };
          const icon = icons[t.status] || "⏳";
          return `<div class="track-item ${t.status}">${icon} ${escapeHtml(t.name || "Carregando...")}</div>`;
        }).join("")}</div>`
      : "";

    const downloadActions = (task.status === "completed" && task.file_url)
      ? `<div class="card-actions">
          <button class="card-action-btn download-link" onclick="triggerDownload('${escapeHtml(task.file_url)}', '')">
            ⬇ Baixar novamente
          </button>
        </div>`
      : "";

    const zipActions = (task.status === "completed" && task.zip_url)
      ? `<div class="card-actions">
          <button class="card-action-btn download-link" onclick="triggerDownload('${escapeHtml(task.zip_url)}', '')">
            📦 Baixar ZIP
          </button>
        </div>`
      : "";

    card.innerHTML = `
      <div class="card-header">
        <div class="card-info">
          <div class="card-type">${typeIcon} ${typeName}</div>
          <div class="card-url" title="${escapeHtml(task.url || "")}">${escapeHtml(truncateUrl(task.url || ""))}</div>
          ${trackInfo}
        </div>
        <div class="card-status">
          <span class="status-badge ${task.status}">${statusLabel(task.status)}</span>
          ${showCancel ? `<button class="cancel-btn" onclick="cancelDownload('${task.id}')" title="Cancelar">✕</button>` : ""}
        </div>
      </div>
      ${
        showProgress
          ? `
        <div class="progress-container">
          <div class="progress-bar-bg">
            <div class="progress-bar-fill" style="width: ${task.progress || 0}%"></div>
          </div>
          <div class="progress-text">
            <span>${progressLabel}</span>
            <span class="progress-percent">${task.progress || 0}%</span>
          </div>
        </div>
      `
          : ""
      }
      ${tracksHtml}
      ${errorsHtml}
      ${downloadActions}
      ${zipActions}
    `;
  });

  Array.from(downloadList.children).forEach((child) => {
    if (child.id !== "queue-empty" && !existingIds.has(child.id)) {
      child.remove();
    }
  });
}

// ─── Toast Notifications ────────────────────────────────────────────────────

function showToast(message, type = "success") {
  const container = document.getElementById("toast-container");
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;

  const icon = type === "success" ? "✅" : type === "error" ? "❌" : "ℹ️";
  toast.innerHTML = `<span class="toast-icon">${icon}</span> ${escapeHtml(message)}`;

  container.appendChild(toast);

  setTimeout(() => {
    toast.classList.add("leaving");
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

// ─── Utilities ──────────────────────────────────────────────────────────────

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function truncateUrl(url) {
  if (url.length <= 60) return url;
  return url.substring(0, 57) + "...";
}

function statusLabel(status) {
  const labels = {
    queued: "Na fila",
    downloading: "Baixando",
    completed: "Concluído",
    error: "Erro",
    cancelled: "Cancelado",
  };
  return labels[status] || status;
}

// ─── Initialize ─────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  refreshQueue();
});
