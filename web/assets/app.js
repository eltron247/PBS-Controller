/* One interface for Windows WebView2 and any browser; no build tooling. */
"use strict";
const $ = (id) => document.getElementById(id);
const TOKEN_KEY = "pbs-controller-token";
const MAX_CONSOLE_CHARS = 2000000;
let token = sessionStorage.getItem(TOKEN_KEY) || "";
let offset = null;
let fileId = null;
let currentState = null;
let polling = false;
let logTick = 0;
let authenticated = false;

function asTime(iso) {
  return iso ? new Date(iso).toLocaleString() : "—";
}
function duration(s) {
  if (s === null || s === undefined) return "—";
  s = Math.floor(s);
  const h = String(Math.floor(s / 3600)).padStart(2, "0");
  const m = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const sec = String(s % 60).padStart(2, "0");
  return h + ":" + m + ":" + sec;
}
async function api(path, options) {
  const opts = Object.assign({ cache: "no-store" }, options || {});
  opts.headers = Object.assign({ "Authorization": "Bearer " + token }, opts.headers || {});
  const response = await fetch(path, opts);
  if (!response.ok) {
    let detail = response.status + " " + response.statusText;
    try {
      const error = await response.json();
      detail = error.detail || detail;
    } catch (_ignored) {}
    const err = new Error(detail);
    err.status = response.status;
    throw err;
  }
  return response;
}
async function json(path, options) {
  return (await api(path, options)).json();
}
function connected(yes, description) {
  $("connection-indicator").classList.toggle("connected", yes);
  $("connection-text").textContent = description || (yes ? "Secure session" : "Disconnected");
  $("footer-state").textContent = yes ? "Authenticated controller session" : "No active session";
}
function showLogin(message) {
  authenticated = false;
  connected(false, "Not connected");
  $("login-overlay").hidden = false;
  $("login-error").textContent = message || "";
  $("start").disabled = true;
  $("stop").disabled = true;
  $("download").disabled = true;
}
function showError(err) {
  $("console-status").textContent = "Connection error: " + err.message;
  connected(false, "Connection issue");
  if (err.status === 401) {
    sessionStorage.removeItem(TOKEN_KEY);
    token = "";
    showLogin("Access token rejected. Check the token and reconnect.");
  }
}
function setStatus(data) {
  currentState = data;
  const sys = data.systemd;
  const run = data.run;
  const running = Boolean(sys.running);
  const status = data.state;
  const outcome = run ? run.status : status;
  const label = status === "idle" && run && run.status === "completed"
    ? "IDLE" : status.toUpperCase();
  $("job-state").textContent = label;
  $("state-dot").className = "state-dot " + (running ? "running" : outcome);
  $("job-detail").textContent = running
    ? "Backup process active on PBS"
    : (run ? "Last controller job: " + run.status : "Systemd service is idle");
  $("elapsed").textContent = running ? duration(sys.elapsed_seconds) :
    (run && run.finished_at ? duration((Date.parse(run.finished_at) - Date.parse(run.started_at)) / 1000) : "—");
  $("last-started").textContent = run ? asTime(run.started_at) : "—";
  $("last-outcome").textContent = run ? "Run #" + run.id + " · " + run.status : "No controller runs recorded";
  $("exit-status").textContent = running ? "—" :
    (run && run.exit_code !== null ? String(run.exit_code) : "—");
  $("log-path").textContent = data.log_path;
  $("start").disabled = running || status === "starting" || (run && ["starting", "stopping"].includes(run.status));
  $("stop").disabled = !running;
  $("download").disabled = false;
  $("footer-state").textContent = sys.sub_state + " / " + (sys.result || "unknown");
}
function appendConsole(text) {
  const consoleView = $("console");
  const follow = $("follow").checked;
  const wasBottom = consoleView.scrollTop + consoleView.clientHeight >= consoleView.scrollHeight - 50;
  if (text) {
    consoleView.textContent += text;
    if (consoleView.textContent.length > MAX_CONSOLE_CHARS) {
      consoleView.textContent = "[Earlier lines omitted from display; download log for complete run]\n" +
        consoleView.textContent.slice(-MAX_CONSOLE_CHARS);
    }
  }
  if (follow && wasBottom) consoleView.scrollTop = consoleView.scrollHeight;
}
async function pollLog() {
  let count = 0;
  let result;
  do {
    const params = new URLSearchParams();
    if (offset !== null) params.set("offset", String(offset));
    if (fileId !== null) params.set("file_id", fileId);
    result = await json("/api/log?" + params.toString());
    if (result.reset) $("console").textContent = "[Log file changed or truncated; resumed from current file]\n";
    offset = result.offset;
    fileId = result.file_id;
    appendConsole(result.text);
    count++;
  } while (result.more && count < 16);
  $("console-status").textContent = result.more
    ? "Catching up with server log…"
    : "Following server log · " + new Date().toLocaleTimeString();
}
async function poll() {
  if (!authenticated || polling) return;
  polling = true;
  try {
    const data = await json("/api/status");
    setStatus(data);
    if (++logTick % 1 === 0) await pollLog();
    connected(true);
  } catch (err) {
    showError(err);
  } finally {
    polling = false;
  }
}
async function signIn(candidate) {
  token = candidate.trim();
  $("login-error").textContent = "";
  try {
    const data = await json("/api/status");
    authenticated = true;
    sessionStorage.setItem(TOKEN_KEY, token);
    $("login-overlay").hidden = true;
    $("console").textContent = "";
    offset = null;
    fileId = null;
    setStatus(data);
    connected(true);
    await pollLog();
  } catch (err) {
    showLogin("Unable to connect: " + err.message);
  }
}
$("login-form").addEventListener("submit", async function (event) {
  event.preventDefault();
  await signIn($("token").value);
});
$("start").addEventListener("click", async function () {
  $("start").disabled = true;
  try {
    const result = await json("/api/start", { method: "POST" });
    $("console").textContent = "";
    offset = result.log_offset;
    fileId = result.log_file_id;
    $("console-status").textContent = "Backup start requested · waiting for output…";
    await poll();
  } catch (err) {
    window.alert("Unable to start backup: " + err.message);
    await poll();
  }
});
$("stop").addEventListener("click", async function () {
  if (!window.confirm("Stop the running backup? This will terminate the backup process and may leave a partial snapshot.")) return;
  $("stop").disabled = true;
  try {
    await json("/api/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true }),
    });
    await poll();
  } catch (err) {
    window.alert("Unable to stop backup: " + err.message);
    await poll();
  }
});
$("clear").addEventListener("click", function () {
  $("console").textContent = "";
  $("console-status").textContent = "Display cleared (server log unchanged)";
});
$("history-toggle").addEventListener("click", async function () {
  $("history-panel").hidden = false;
  try {
    const result = await json("/api/history?limit=30");
    const list = $("history-list");
    list.textContent = "";
    if (!result.runs.length) {
      list.textContent = "No controller runs recorded yet.";
      return;
    }
    result.runs.forEach(function (run) {
      const row = document.createElement("div");
      row.className = "history-row";
      const id = document.createElement("span");
      id.textContent = "#" + run.id;
      const date = document.createElement("span");
      date.textContent = asTime(run.started_at);
      const status = document.createElement("span");
      status.textContent = run.status;
      status.className = "status-" + run.status;
      const code = document.createElement("span");
      code.textContent = run.exit_code === null ? "—" : "exit " + run.exit_code;
      row.append(id, date, status, code);
      list.append(row);
    });
  } catch (err) {
    $("history-list").textContent = "Could not load history: " + err.message;
  }
});
$("history-close").addEventListener("click", function () {
  $("history-panel").hidden = true;
});
$("download").addEventListener("click", async function () {
  try {
    const response = await api("/api/log/download");
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "pbs-backup-run.log";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  } catch (err) {
    window.alert("Unable to download log: " + err.message);
  }
});
setInterval(poll, 1000);
if (token) signIn(token);
else showLogin("");
