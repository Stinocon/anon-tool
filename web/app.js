/* anon-tool UI — vanilla, no dependencies, no CDN (works offline).
   One primary action per tab; everything secondary lives behind a <details>.
   The page only ever talks to its own origin, always with the per-run token header. */

const TOKEN = window.ANON_TOKEN;
const $ = (id) => document.getElementById(id);
const state = { maps: [], selectedMap: null, anonFile: null, anonQueue: [], deanonFile: null, entitiesFile: "entities", entitiesLoaded: "", lastMapId: null, maxUploadBytes: null, suggest: false, suggestMaxChars: null, suggestTimeout: null, suggestChunkChars: 0, suggestChunkOverlap: 0, suggestions: [], version: null, build: null, lastReport: null, lastReportName: "" };

const api = (path, options = {}) =>
  fetch(path, { ...options, headers: { "X-Anon-Token": TOKEN, ...(options.headers || {}) } });

async function request(path, options) {
  const response = await api(path, options);
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok) throw new Error(payload.error || i18n.t("error.request", { status: response.status }));
  return payload;
}

/* Il modello risponde a pezzi e il server li manda come server-sent events: `start`, tanti
   `delta`, poi `done`. I delta sono AVANZAMENTO, non un verdetto — il report arriva solo alla
   fine, quando il server ha analizzato e localizzato l'intera risposta — quindi non possono
   anticipare un valore che poi non verra' proposto. */
function parseEventFrame(frame) {
  const line = frame.split("\n").find((row) => row.startsWith("data:"));
  if (!line) return null;
  try {
    return JSON.parse(line.slice(5).trim());
  } catch {
    return null;
  }
}

async function readEventStream(body, onDelta) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let report = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let split = buffer.indexOf("\n\n"); // un frame SSE finisce con una riga vuota
    while (split !== -1) {
      const event = parseEventFrame(buffer.slice(0, split));
      buffer = buffer.slice(split + 2);
      if (event && event.event === "delta" && typeof event.text === "string") onDelta(event.text);
      else if (event && event.event === "done") report = event.report || null;
      else if (event && event.event === "error") throw new Error(event.message || i18n.t("suggest.streamBroken"));
      split = buffer.indexOf("\n\n");
    }
  }
  return report;
}

/** Lo stesso report di `/api/suggest`, con i pezzi mostrati mentre arrivano. */
async function suggestReport(payload, onDelta) {
  const response = await api("/api/suggest-stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(payload),
  });
  if (!response.ok || !response.body || !response.body.getReader) {
    // Un rifiuto PRIMA dello stream (nessun modello configurato, testo mancante) e' un errore
    // normale con il suo JSON; un browser senza stream ricade sulla chiamata bloccante.
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || i18n.t("error.request", { status: response.status }));
    return request("/api/suggest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  }
  const report = await readEventStream(response.body, onDelta);
  if (!report) throw new Error(i18n.t("suggest.streamBroken"));
  return report;
}

/* ---------------------------------------------------------------- helpers */
function setStatus(element, message, kind = "") {
  element.textContent = message || "";
  element.className = `status${kind ? ` ${kind}` : ""}`;
}

/* ------------------------------------------------------------- avanzamento */
const humanSize = (bytes) => {
  const mb = bytes / (1024 * 1024);
  if (mb >= 1) return `${mb >= 100 ? Math.round(mb) : mb.toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} kB`;
};

// Two bars reuse this code: the anonymization one and the model one, in different tabs. Each bar
// keeps its OWN timer and start time — a single shared state made a suggest run stop the
// anonymization bar, the upload's percentage write into the wrong bar, and vice versa.
const PROGRESS_MIN_MS = 700; // a bar that blinks for 200ms is worse than none: keep it perceptible
const PROGRESS_ANON = "anon-progress";
const PROGRESS_SUGGEST = "suggest-progress";
const progressState = new Map(); // bar id -> { timer, shownAt }

function progressStateOf(bar) {
  let state = progressState.get(bar);
  if (!state) {
    state = { timer: null, shownAt: 0 };
    progressState.set(bar, state);
  }
  return state;
}

function progressStart(label, bar = PROGRESS_ANON) {
  const barState = progressStateOf(bar);
  // A previous `progressWait` may still be ticking: a queue runs many files through one bar, and
  // the stale interval would keep overwriting the label with the PREVIOUS file's seconds.
  clearInterval(barState.timer);
  barState.timer = null;
  barState.shownAt = Date.now();
  $(bar).hidden = false;
  $(bar).classList.remove("is-waiting");
  $(`${bar}-fill`).style.width = "2%";
  $(`${bar}-fill`).textContent = "";
  $(`${bar}-label`).textContent = label;
}

function progressPercent(fraction, bar = PROGRESS_ANON) {
  const percent = Math.max(0, Math.min(100, Math.round(fraction * 100)));
  $(`${bar}-fill`).style.width = `${percent}%`;
  $(`${bar}-fill`).textContent = `${percent}%`;
}

/** The conversion and the anonymization happen inside ONE request, so there is no percentage to
    report while they run. The bar stops claiming one and shows elapsed time instead of a lie. */
function progressWait(label, bar = PROGRESS_ANON) {
  const state = progressStateOf(bar);
  $(bar).classList.add("is-waiting");
  const started = Date.now();
  const tick = () => {
    const seconds = Math.round((Date.now() - started) / 1000);
    $(`${bar}-label`).textContent = `${label} — ${seconds}s`;
  };
  tick();
  clearInterval(state.timer);
  state.timer = setInterval(tick, 1000);
}

function progressStop(bar = PROGRESS_ANON) {
  const state = progressStateOf(bar);
  clearInterval(state.timer);
  state.timer = null;
  const shown = Date.now() - state.shownAt;
  if (shown < PROGRESS_MIN_MS) {
    // Hide LATER, not now: the point is that the operator sees the phases, not that the element
    // is gone as fast as possible. The bar is captured so the deferred stop stops THIS bar only.
    setTimeout(() => progressStop(bar), PROGRESS_MIN_MS - shown);
    return;
  }
  $(bar).hidden = true;
  $(bar).classList.remove("is-waiting");
  $(`${bar}-fill`).style.width = "0%";
  $(`${bar}-fill`).textContent = "";
  $(`${bar}-label`).textContent = "";
}

/** POST that reports upload progress — `fetch` cannot. */
function requestWithProgress(path, headers, body, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    xhr.setRequestHeader("X-Anon-Token", TOKEN);
    for (const [name, value] of Object.entries(headers)) xhr.setRequestHeader(name, value);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.onload = () => {
      let payload = {};
      try {
        payload = JSON.parse(xhr.responseText);
      } catch {
        payload = {};
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(payload);
      else reject(new Error(payload.error || i18n.t("error.request", { status: xhr.status })));
    };
    xhr.onerror = () => reject(new Error(i18n.t("error.network")));
    xhr.send(body);
  });
}

function show(button, busy, busyLabel) {
  if (busy) {
    button.dataset.label = button.textContent;
    button.textContent = busyLabel || i18n.t("busy.default");
    button.disabled = true;
  } else {
    if (button.dataset.label) button.textContent = button.dataset.label;
    button.disabled = false;
  }
}

function chips(container, counts) {
  container.innerHTML = "";
  const entries = Object.entries(counts || {}).sort();
  if (!entries.length) {
    const span = document.createElement("span");
    span.className = "chip chip-zero";
    span.textContent = i18n.t("chips.none");
    container.append(span);
    return;
  }
  for (const [type, count] of entries) {
    const span = document.createElement("span");
    span.className = "chip";
    span.textContent = `${type} × ${count}`;
    container.append(span);
  }
}

// A placeholder exactly as the engine writes it. Kept identical to anon.py's PLACEHOLDER_RE in
// SHAPE (the tag may widen to 8 hex when 6 is taken), never to a number in one place.
const PLACEHOLDER_RE = /\[([A-Z][A-Z0-9_]*)-(\d+)-([0-9a-f]{6,8})\]/g;

/* Where the redaction actually landed, without a single real value: every placeholder becomes a
   pill labelled with its TYPE, the rest is plain text. Built with DOM nodes and `textContent`
   ONLY — the redacted text comes from the operator's document, and the file's rule is that text we
   did not write never goes through `innerHTML`. */
function renderHighlight(text) {
  const box = $("highlight-view");
  if (!box) return;
  box.innerHTML = "";
  let last = 0;
  const appendText = (chunk) => {
    const span = document.createElement("span");
    span.textContent = chunk;
    box.append(span);
  };
  for (const match of String(text).matchAll(PLACEHOLDER_RE)) {
    if (match.index > last) appendText(text.slice(last, match.index));
    const pill = document.createElement("span");
    pill.className = "ph-ev";
    pill.dataset.type = match[1];
    pill.textContent = match[1];
    pill.title = match[0];
    box.append(pill);
    last = match.index + match[0].length;
  }
  if (last < text.length) appendText(text.slice(last));
}

/* The report a consultant attaches to a delivered document: what was substituted, by type, under
   which map — and NOT one real value. Every field here comes from the anonymize response (counts,
   the placeholder list, the map id), never from the reveal, so leaking is not a matter of care. */
function redactionReport(name, result) {
  const counts = Object.entries(result.counts || {}).sort();
  const build = String(state.build || "?").slice(0, 8);
  const lines = [
    `# ${i18n.t("report.title")}`,
    "",
    `- ${i18n.t("report.file")}: \`${name}\``,
    `- ${i18n.t("report.tool")}: anon-tool ${state.version || "?"} · build ${build}`,
    `- ${i18n.t("report.date")}: ${new Date().toISOString()}`,
    `- ${i18n.t("report.map")}: \`${result.map_id || "—"}\``,
    "",
    `## ${i18n.t("report.counts")}`,
    "",
    `| ${i18n.t("report.type")} | ${i18n.t("report.count")} |`,
    "|---|---|",
    ...counts.map(([type, n]) => `| ${type} | ${n} |`),
    "",
    `## ${i18n.t("report.placeholders")}`,
    "",
    ...(result.entries || []).map((entry) => `- \`${entry.placeholder}\` — ${entry.type}`),
    "",
    `> ${i18n.t("report.note")}`,
    "",
  ];
  return lines.join("\n");
}

function saveBlob(name, blob) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}

function download(name, text, base64) {
  const blob = base64
    ? new Blob([Uint8Array.from(atob(base64), (c) => c.charCodeAt(0))], { type: "application/octet-stream" })
    : new Blob([text], { type: "text/plain;charset=utf-8" });
  saveBlob(name, blob);
}

/* The redacted document is fetched from its own URL instead of being carried inside the JSON: the
   server streams it in blocks and holds one of them at a time, where base64 in the response body
   would build more than the file itself as a string. The fetch still carries the per-run token. */
async function downloadFromServer(url, name) {
  const response = await api(url);
  if (!response.ok) throw new Error(i18n.t("download.failed", { status: response.status }));
  saveBlob(name, await response.blob());
}

function dropzone(zone, input, onFiles) {
  zone.addEventListener("dragover", (event) => {
    event.preventDefault();
    zone.classList.add("is-over");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("is-over"));
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    zone.classList.remove("is-over");
    if (event.dataTransfer.files.length) onFiles([...event.dataTransfer.files]);
  });
  if (input) input.addEventListener("change", () => input.files.length && onFiles([...input.files]));
}

const selected = (selector) =>
  [...document.querySelectorAll(selector)].filter((input) => input.checked).map((input) => input.value);

const stripExtension = (name) => name.replace(/\.[^.]+$/, "");
const extensionOf = (name) => (name.match(/\.[^.]+$/) || [".txt"])[0];
const isDocument = (name) => /\.(docx?|xlsx?|pptx?|odt|ods|odp|pdf|rtf|epub)$/i.test(name);

/* ---------------------------------------------------------------- tabs */
function activateTab(tab) {
  document.querySelectorAll(".tab").forEach((candidate) => {
    const active = candidate === tab;
    candidate.classList.toggle("is-active", active);
    candidate.setAttribute("aria-selected", String(active));
    $(candidate.getAttribute("aria-controls")).classList.toggle("is-active", active);
  });
}
document.querySelectorAll(".tab").forEach((tab, index, all) => {
  tab.addEventListener("click", () => activateTab(tab));
  tab.addEventListener("keydown", (event) => {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const next = all[(index + step + all.length) % all.length];
    activateTab(next);
    next.focus();
  });
});

/* ------------------------------------------------------------------ i18n
 * i18n.js is loaded before this file. If it is missing (a stale cache, a partial deploy) the page
 * must not throw on the first t(). This fallback covers only the keys it LISTS and returns the key
 * for the rest, so a missing dictionary degrades to key names — not to Italian, which lives in
 * i18n.js as the one source, and not to a dead page. */
const i18n = (() => {
  if (window.AnonI18n) return window.AnonI18n;
  const italian = {
    options: "Opzioni",
    "theme.dark": "Tema: scuro",
    "theme.light": "Tema: chiaro",
    "theme.toDark": "Passa al tema scuro",
    "theme.toLight": "Passa al tema chiaro",
  };
  return { t: (key) => italian[key] ?? key, init: () => "it", apply: () => "it", current: "it" };
})();

/* ---------------------------------------------------------------- tema */
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const button = $("theme-toggle");
  const light = theme === "light";
  // Le stringhe passano dall'i18n: la lingua scelta vale anche qui, e il cambio lingua
  // richiama applyTheme per non lasciare il pulsante in italiano su una pagina in inglese.
  button.textContent = light ? i18n.t("theme.light") : i18n.t("theme.dark");
  button.setAttribute("aria-pressed", String(light));
  button.title = light ? i18n.t("theme.toDark") : i18n.t("theme.toLight");
  try {
    localStorage.setItem("anon-theme", theme);
  } catch {
    /* private mode: the choice simply does not persist */
  }
}
$("theme-toggle").addEventListener("click", () =>
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light"));
applyTheme(document.documentElement.dataset.theme || "dark");

/* ---------------------------------------------------------------- state */
function optionsSummary() {
  const patterns = selected(".pattern");
  const catalogs = selected(".catalog");
  const parts = [
    patterns.length === 3
      ? i18n.t("summary.allPatterns")
      : patterns.length
        ? i18n.t("summary.patterns", { list: patterns.join(", ") })
        : i18n.t("summary.noPatterns"),
    catalogs.length
      ? i18n.t("summary.catalogs", { list: catalogs.join(", ") })
      : i18n.t("summary.noCatalogs"),
  ];
  $("options-summary").textContent = `${i18n.t("options")} — ${parts.join(" · ")}`;
}

function refreshButtons() {
  $("run-anon").disabled = !$("text-anon").value.trim() && !state.anonFile && !state.anonQueue.length;
  $("clear-anon").hidden = !$("text-anon").value && !state.anonFile && !state.anonQueue.length;
  $("run-deanon").disabled = !state.deanonFile || !state.selectedMap;
  $("run-audit").disabled = !$("text-audit").value.trim();
  $("save-entities").disabled = $("entities-text").value === state.entitiesLoaded;
}

async function boot() {
  const info = await request("/api/state");
  // Version AND the build fingerprint: two builds of one version are otherwise indistinguishable,
  // and a container left behind after a fix then looks current. The full digest is in the tooltip.
  $("version").textContent = `v${info.version} · ${info.schema} · ${String(info.build || "?").slice(0, 8)}`;
  $("version").title = info.build ? `build ${info.build}` : "";
  state.version = info.version;
  state.build = info.build;
  const converter = $("converter-badge");
  converter.textContent = info.converter ? i18n.t("converter.badge.yes") : i18n.t("converter.badge.no");
  converter.className = info.converter ? "badge badge-ok" : "badge badge-warn";
  converter.title = info.converter
    ? i18n.t("converter.yes")
    : i18n.t("converter.no");
  $("entities-path").textContent = info.entities_path;
  // The upload cap belongs to the server: the UI states it instead of keeping its own copy.
  state.maxUploadBytes = info.max_upload_bytes || null;
  if (state.maxUploadBytes) $("upload-limit").textContent = humanSize(state.maxUploadBytes);

  // Il seam resta SPENTO finche' il server non e' avviato con --suggest-url/--suggest-model, ma il
  // pannello si vede lo stesso e dice come accenderlo: una funzione che non si trova non esiste, e
  // nascondere il pannello era il modo di non farla trovare.
  state.suggest = Boolean(info.suggest);
  $("suggest-fields").hidden = !state.suggest;
  $("suggest-off").hidden = state.suggest;
  $("suggest-state").textContent = state.suggest ? i18n.t("suggest.configured") : i18n.t("suggest.unconfigured");
  $("suggest-state").className = state.suggest ? "badge badge-ok" : "badge badge-warn";
  if (state.suggest) $("suggest-backend").textContent = info.suggest_backend || i18n.t("suggest.localModel");
  // I limiti del modello vengono dal server, non da una copia qui: la finestra e il timeout sono
  // suoi, e una seconda copia li farebbe divergere senza che nessuno se ne accorga.
  state.suggestMaxChars = info.suggest_max_chars || null;
  state.suggestTimeout = info.suggest_timeout || null;
  // Chunking is the server's configuration, not a knob the page owns: the panel only states it.
  state.suggestChunkChars = info.suggest_chunk_chars || 0;
  state.suggestChunkOverlap = info.suggest_chunk_overlap || 0;
  updateSuggestLimits();
  updateSuggestCount();

  state.converter = Boolean(info.converter);
  state.suggestBackend = info.suggest_backend || "";

  const catalogs = info.catalogs || [];
  $("catalogs").innerHTML = "";
  if (!catalogs.length) {
    const empty = document.createElement("span");
    empty.className = "muted small";
    empty.textContent = i18n.t("catalogs.none");
    $("catalogs").append(empty);
  }
  for (const catalog of catalogs) {
    const label = document.createElement("label");
    label.innerHTML =
      `<input type="checkbox" class="catalog" value="${catalog.name}"> ` +
      `<span>${catalog.name}</span> <span class="muted">${i18n.t("catalogs.entries", { n: catalog.entries })}</span>`;
    label.querySelector("input").addEventListener("change", optionsSummary);
    $("catalogs").append(label);
  }
  optionsSummary();
  await loadMaps();
  await loadEntities();
}

/* ---------------------------------------------------------------- mappe */
async function loadMaps() {
  const { maps = [], total = maps.length, truncated = false } = await request("/api/maps");
  state.maps = maps;
  const list = $("map-list");
  list.innerHTML = "";
  if (truncated) {
    // The list is capped: saying "100 mappe" when there are 300 would be a silent lie.
    const note = document.createElement("p");
    note.className = "map-empty";
    note.textContent = i18n.t("maps.truncated", { shown: maps.length, total });
    list.append(note);
  }
  if (!maps.length) {
    const empty = document.createElement("p");
    empty.className = "map-empty";
    empty.textContent = i18n.t("maps.none");
    list.append(empty);
    return;
  }
  for (const map of maps) {
    if (map.unreadable) {
      // A corrupt map is shown, not hidden: skipping it silently was why the list disagreed
      // with the total. It cannot be selected — restoring from it would fail anyway.
      const broken = document.createElement("p");
      broken.className = "map-empty";
      broken.textContent = i18n.t("maps.broken", { id: map.id });
      list.append(broken);
      continue;
    }
    const card = document.createElement("label");
    card.className = "map-card";
    const counts = Object.entries(map.counts || {}).map(([type, n]) => `${type}×${n}`).join(" ") || "—";
    card.innerHTML =
      `<input type="radio" name="map" value="${map.id}">` +
      `<span><span class="map-title">${map.id}</span><br>` +
      `<span class="map-meta">${(map.created || "").slice(0, 16).replace("T", " ")} · ` +
      `${i18n.t("maps.entries", { n: map.entries })} · ${counts}${map.source ? ` · ${map.source}` : ""}</span></span>`;
    const radio = card.querySelector("input");
    radio.addEventListener("change", () => {
      state.selectedMap = radio.value;
      document.querySelectorAll(".map-card").forEach((other) => other.classList.remove("is-selected"));
      card.classList.add("is-selected");
      refreshButtons();
    });
    list.append(card);
  }
  if (state.lastMapId) {
    const radio = list.querySelector(`input[value="${state.lastMapId}"]`);
    if (radio) {
      radio.checked = true;
      radio.dispatchEvent(new Event("change"));
    }
  }
}

/* ------------------------------------------------------------ anonimizza */
/** Una sola anonimizzazione, qualunque sia la sorgente: un documento che il server converte, o
    del testo. Restituisce `{ result, baseName }`, tutto cio' che serve a mostrare o archiviare
    l'esito. Estratta perche' una coda di file e un file singolo eseguano lo STESSO codice: una
    seconda copia e' il posto in cui i due percorsi smettono di essere d'accordo. */
async function anonymizeSource({ file, text }) {
  if (file && isDocument(file.name)) {
    progressStart(i18n.t("progress.loading", { name: file.name, size: humanSize(file.size) }));
    const result = await requestWithProgress(
      "/api/anonymize-document",
      {
        "X-Filename": file.name,
        "X-Catalogs": selected(".catalog").join(","),
        "X-Patterns": selected(".pattern").join(","),
        "Content-Type": "application/octet-stream",
      },
      await file.arrayBuffer(),
      (fraction) => {
        if (fraction >= 1) progressWait(i18n.t("progress.converting"));
        else progressPercent(fraction);
      },
    );
    return { result, baseName: `${stripExtension(file.name)}.redacted.md` };
  }
  const result = await request("/api/anonymize", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      text: file ? await file.text() : text,
      catalogs: selected(".catalog"),
      patterns: selected(".pattern"),
    }),
  });
  return { result, baseName: `${file ? stripExtension(file.name) : i18n.t("filename.text")}.redacted.txt` };
}

/** Scrive la scheda dettagliata (evidenziazione, mappa, download) a partire da UN esito. La coda
    tiene la sua fotografia per file, quindi questa descrive solo l'ULTIMO mostrato. */
function presentResult(name, result, baseName) {
  state.lastMapId = result.map_id;
  pending = {
    text: result.redacted,
    name: baseName,
    containerUrl: result.container_url || null,
    containerName: result.container_name || "",
  };
  $("anon-result").hidden = false;
  $("anon-result-title").textContent = i18n.t("result.title", { name });
  chips($("anon-counts"), result.counts);
  $("redacted").value = result.redacted;
  renderHighlight(result.redacted);
  state.lastReport = redactionReport(name, result);
  state.lastReportName = `${stripExtension(baseName)}.report.md`;
  $("download-report").hidden = false;
  // The document itself, when the upload was one we can rewrite. One redaction produced both
  // artifacts and one map, so they can never disagree; the button is absent when there is
  // nothing to hand back (a PDF, a container we cannot open, or nothing to redact).
  $("download-document").hidden = !pending.containerUrl;
  $("download-document").title = pending.containerUrl
    ? i18n.t("result.sameMap", { name: pending.containerName })
    : "";
  $("mapping").innerHTML = `<span class="muted small">${i18n.t("mapping.notShown")}</span>`;
}

/* --------------------------------------------------------------- coda file */
/* La coda e' SEQUENZIALE e lato client: il server resta one-shot, ogni file ha la sua chiamata,
   la sua mappa e i suoi artefatti. Nessuno stato condiviso fra i file: l'esito di ciascuno e'
   fotografato sulla sua riga quando arriva, cosi' il file B non puo' mostrare la mappa di A. */
function queueButton(label, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

function queueRow(item) {
  const row = document.createElement("div");
  row.className = "queue-item";
  row.dataset.state = item.status;

  const head = document.createElement("div");
  head.className = "queue-head";
  const name = document.createElement("span");
  name.className = "queue-name";
  name.textContent = item.file.name;
  const status = document.createElement("span");
  status.className = "queue-state";
  status.textContent = i18n.t(`queue.${item.status}`);
  head.append(name, status);
  row.append(head);

  if (item.status === "error") {
    const message = document.createElement("p");
    message.className = "queue-message";
    message.textContent = item.message || "";
    row.append(message);
    return row;
  }
  if (item.status !== "done") return row;

  const body = document.createElement("div");
  body.className = "queue-body";
  const counts = document.createElement("div");
  counts.className = "chips";
  chips(counts, item.result.counts);
  body.append(counts);

  const actions = document.createElement("div");
  actions.className = "queue-actions";
  if (item.result.container_url) {
    actions.append(queueButton(i18n.t("queue.document"), async () => {
      await downloadFromServer(item.result.container_url, item.result.container_name);
    }));
  }
  actions.append(queueButton(i18n.t("queue.text"), () => download(item.baseName, item.result.redacted)));
  actions.append(queueButton(i18n.t("queue.report"), () => download(item.reportName, item.report)));
  body.append(actions);
  row.append(body);
  return row;
}

function renderQueue() {
  const card = $("anon-queue-card");
  const box = $("anon-queue");
  const items = state.anonQueue;
  card.hidden = items.length === 0;
  box.innerHTML = "";
  for (const item of items) box.append(queueRow(item));
  const done = items.filter((item) => item.status === "done").length;
  $("anon-queue-summary").textContent = items.length
    ? i18n.t("queue.summary", { done, total: items.length })
    : "";
}

async function runQueue() {
  let lastOk = null;
  const total = state.anonQueue.length;
  for (const item of state.anonQueue) {
    // Un file gia' fatto conserva la sua mappa e i suoi artefatti: riprocessarlo creerebbe una
    // seconda mappa per lo stesso file e, se fallisse, trasformerebbe una riga con i download in
    // un errore. Solo cio' che non e' ancora riuscito viene eseguito.
    if (item.status === "done") continue;
    item.status = "running";
    renderQueue();
    // Un file di testo non ha una percentuale da mostrare: se la barra del documento precedente
    // restasse a video, mostrerebbe l'etichetta sbagliata mentre gira questo file.
    if (!isDocument(item.file.name)) progressStop();
    const done = state.anonQueue.filter((other) => other.status === "done").length;
    setStatus($("anon-status"), i18n.t("queue.progress", { done: done + 1, total }));
    try {
      const { result, baseName } = await anonymizeSource({ file: item.file });
      item.result = result;
      item.baseName = baseName;
      const name = result.container_name || baseName;
      item.report = redactionReport(name, result);
      item.reportName = `${stripExtension(baseName)}.report.md`;
      item.status = "done";
      lastOk = { name, result, baseName };
    } catch (error) {
      item.status = "error";
      item.message = String(error.message || error);
    }
    renderQueue();
  }
  if (lastOk) presentResult(lastOk.name, lastOk.result, lastOk.baseName);
  const ok = state.anonQueue.filter((item) => item.status === "done").length;
  const failed = state.anonQueue.filter((item) => item.status === "error").length;
  if (failed) {
    setStatus(
      $("anon-status"),
      i18n.t("queue.finished", { ok, total }) + i18n.t("queue.failed", { n: failed }),
      "error",
    );
  } else {
    setStatus($("anon-status"), i18n.t("queue.finished", { ok, total }), "ok");
  }
  if (lastOk) $("anon-result").scrollIntoView({ block: "nearest" });
  await loadMaps();
}

$("run-anon").addEventListener("click", async () => {
  const button = $("run-anon");
  show(button, true);
  setStatus($("anon-status"), i18n.t("progress.processing"));
  try {
    if (state.anonQueue.length) {
      await runQueue();
      return;
    }
    const { result, baseName } = await anonymizeSource({ file: state.anonFile, text: $("text-anon").value });
    presentResult(result.container_name || baseName, result, baseName);
    if (result.container_error) {
      setStatus($("anon-status"), i18n.t("status.notRewritable", { detail: result.container_error }), "warn");
    } else {
      setStatus($("anon-status"), i18n.t("status.rulesApplied", { n: result.rules_applied }), "ok");
    }
    $("anon-result").scrollIntoView({ block: "nearest" });
    await loadMaps();
  } catch (error) {
    setStatus($("anon-status"), String(error.message || error), "error");
  } finally {
    show(button, false);
    progressStop();
    refreshButtons();
  }
});

let pending = { text: "", name: "redatto.txt", containerUrl: null, containerName: "" };

$("clear-anon").addEventListener("click", () => {
  $("text-anon").value = "";
  $("file-anon").value = "";
  state.anonFile = null;
  state.anonQueue = [];
  renderQueue();
  pending = { text: "", name: "redatto.txt", containerUrl: null, containerName: "" };
  $("download-document").hidden = true;
  progressStop();
  $("anon-result").hidden = true;
  setStatus($("anon-status"), "");
  refreshButtons();
});
$("copy-redacted").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("redacted").value);
  setStatus($("anon-status"), i18n.t("status.copied"), "ok");
});
$("download-redacted").addEventListener("click", () => download(pending.name, pending.text));
$("download-report").addEventListener("click", () => {
  if (state.lastReport) download(state.lastReportName, state.lastReport);
});
$("download-document").addEventListener("click", async () => {
  if (!pending.containerUrl) return;
  setStatus($("anon-status"), i18n.t("progress.downloading"));
  try {
    await downloadFromServer(pending.containerUrl, pending.containerName);
    setStatus($("anon-status"), i18n.t("status.downloaded"), "ok");
  } catch (error) {
    setStatus($("anon-status"), String(error.message || error), "error");
  }
});

/* The reveal view shows the REAL values. They must not keep living in the page because a tab
   was left open: auto-relock after REVEAL_TTL_MS, plus an explicit "hide again". */
const REVEAL_TTL_MS = 60000;
let revealTimer = null;

function relockMapping(message) {
  if (revealTimer) {
    clearTimeout(revealTimer);
    revealTimer = null;
  }
  $("mapping").innerHTML = `<span class="muted small">${message || i18n.t("mapping.relocked")}</span>`;
  $("hide-map").hidden = true;
}

$("reveal-map").addEventListener("click", async () => {
  if (!state.lastMapId) return;
  try {
    const data = await request("/api/maps/reveal", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: state.lastMapId, confirm: true }),
    });
    const mapping = $("mapping");
    mapping.innerHTML = "";
    for (const [placeholder, info] of Object.entries(data.entries)) {
      const row = document.createElement("div");
      row.className = "row";
      row.innerHTML = `<span class="ph">${placeholder}</span><span class="muted">${info.type}</span><span></span>`;
      row.lastChild.textContent = info.original;
      mapping.append(row);
    }
    $("hide-map").hidden = false;
    if (revealTimer) clearTimeout(revealTimer);
    revealTimer = setTimeout(() => relockMapping(i18n.t("mapping.relockedTimeout")), REVEAL_TTL_MS);
  } catch (error) {
    $("mapping").textContent = String(error.message || error);
  }
});

$("hide-map").addEventListener("click", () => relockMapping());

/* ---------------------------------------------------------- deanonimizza */
$("run-deanon").addEventListener("click", async () => {
  const button = $("run-deanon");
  show(button, true);
  setStatus($("deanon-status"), i18n.t("progress.processing"));
  try {
    const result = await request("/api/deanonymize", {
      method: "POST",
      headers: {
        "X-Filename": state.deanonFile.name,
        "X-Map-Id": state.selectedMap,
        "Content-Type": "application/octet-stream",
      },
      body: await state.deanonFile.arrayBuffer(),
    });
    const report = result.report || {};
    const box = $("deanon-report");
    box.hidden = false;
    box.innerHTML = "";
    const rows = [
      [i18n.t("deanon.replaced"), String(report.replaced ?? 0), false],
      [i18n.t("deanon.remaining"), String(report.remaining ?? 0), (report.remaining ?? 0) > 0],
      [i18n.t("deanon.unknown"), String(report.unknown_placeholders ?? 0), (report.unknown_placeholders ?? 0) > 0],
      [i18n.t("deanon.map"), report.map_source || "?", false],
      [i18n.t("deanon.outcome"), report.complete ? i18n.t("deanon.complete") : i18n.t("deanon.incomplete"), !report.complete],
    ];
    for (const [key, value, bad] of rows) {
      const row = document.createElement("div");
      row.className = `row${bad ? " bad" : ""}`;
      row.innerHTML = `<span class="k">${key}</span><span></span>`;
      row.lastChild.textContent = value;
      box.append(row);
    }
    const extension = extensionOf(state.deanonFile.name);
    download(`${stripExtension(state.deanonFile.name)}-finale${extension}`, null, result.content_b64);
    setStatus($("deanon-status"),
      report.complete ? i18n.t("deanon.downloaded") : i18n.t("deanon.incompleteDetail"),
      report.complete ? "ok" : "error");
  } catch (error) {
    setStatus($("deanon-status"), String(error.message || error), "error");
  } finally {
    show(button, false);
    refreshButtons();
  }
});

/* ---------------------------------------------------------------- verifica */
$("run-audit").addEventListener("click", async () => {
  const button = $("run-audit");
  show(button, true);
  setStatus($("audit-status"), i18n.t("progress.checking"));
  try {
    const result = await request("/api/audit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: $("text-audit").value,
        catalogs: selected(".catalog"),
        patterns: selected(".pattern"),
        reveal: $("audit-reveal").checked,
      }),
    });
    $("audit-result").hidden = false;
    const verdict = $("audit-verdict");
    verdict.className = `verdict ${result.verdict}`;
    verdict.textContent =
      result.verdict === "clean"
        ? i18n.t("audit.clean")
        : result.verdict === "sensitive"
          ? i18n.t("audit.sensitive", { n: result.total })
          : i18n.t("audit.suspect");
    chips($("audit-types"), result.types);

    const near = $("audit-near");
    near.innerHTML = "";
    if (result.candidates_capped) {
      // The engine stops the near-miss search at a bound (400 words / 200 entities). Said out
      // loud: a short list would otherwise read as "nothing suspicious" when it is only partial.
      const row = document.createElement("div");
      row.className = "row bad";
      row.innerHTML = `<span class="k">${i18n.t("audit.cappedLabel")}</span><span></span>`;
      row.lastChild.textContent = i18n.t("audit.capped");
      near.append(row);
    }
    if (result.placeholders_present) {
      const row = document.createElement("div");
      row.className = "row";
      row.innerHTML = `<span class="k">placeholder</span><span></span>`;
      row.lastChild.textContent = i18n.t("audit.placeholders", { n: result.placeholders_present });
      near.append(row);
    }
    for (const item of result.near_miss || []) {
      const row = document.createElement("div");
      row.className = "row";
      row.innerHTML = `<span class="k">${i18n.t("audit.line", { n: item.line })}</span><span class="muted">${item.kind} · ${item.type}</span><span></span>`;
      row.lastChild.textContent = item.token ? `${item.token} ~ ${item.entity}` : item.token_masked;
      near.append(row);
    }
    setStatus($("audit-status"), i18n.t("audit.verdict", { verdict: result.verdict }), result.verdict === "clean" ? "ok" : "");
  } catch (error) {
    setStatus($("audit-status"), String(error.message || error), "error");
  } finally {
    show(button, false);
    refreshButtons();
  }
});
$("audit-reveal").addEventListener("change", () => {
  if (!$("audit-result").hidden) $("run-audit").click();
});

/* --------------------------------------------------------------- dizionario */
async function loadEntities() {
  const entities = await request(`/api/entities?file=${encodeURIComponent(state.entitiesFile)}`);
  $("entities-text").value = entities.text || "";
  state.entitiesLoaded = $("entities-text").value;
  $("entities-path").textContent = entities.path;
  refreshButtons();
}

$("entities-file").addEventListener("change", async (event) => {
  const next = event.target.value;
  if ($("entities-text").value !== state.entitiesLoaded) {
    const go = confirm(i18n.t("entities.unsaved"));
    if (!go) {
      event.target.value = state.entitiesFile;
      return;
    }
  }
  const previous = state.entitiesFile;
  state.entitiesFile = next;
  try {
    await loadEntities();
  } catch (error) {
    // Revert on failure, or the download name would describe a file that is not on screen.
    state.entitiesFile = previous;
    event.target.value = previous;
    setStatus($("entities-status"), String(error.message || error), "error");
  }
});

/* ------------------------------------------------- suggerimenti dal modello locale */
/* Il modello PROPONE, l'operatore approva, il dizionario applica: qui non si scrive nulla finche'
   non e' l'operatore a premere Salva sul dizionario. Un valore arriva dal modello e finisce nel DOM
   solo con textContent: mai innerHTML con testo che non abbiamo scritto noi. */
const SUGGEST_TYPES = ["PERSONA", "AZIENDA", "CLIENTE", "SEDE", "ALTRO"];

function renderSuggestions(proposals) {
  const list = $("suggest-list");
  list.textContent = "";
  state.suggestions = proposals;
  $("suggest-actions").hidden = proposals.length === 0;
  for (const [index, proposal] of proposals.entries()) {
    const row = document.createElement("label");
    row.className = "suggest-row";

    const check = document.createElement("input");
    check.type = "checkbox";
    check.checked = true;
    check.dataset.index = String(index);

    const select = document.createElement("select");
    for (const type of SUGGEST_TYPES) {
      const option = document.createElement("option");
      option.value = type;
      option.textContent = type;
      select.append(option);
    }
    select.value = SUGGEST_TYPES.includes(proposal.type) ? proposal.type : "ALTRO";

    const value = document.createElement("code");
    value.className = "suggest-value";
    value.textContent = proposal.value;

    const note = document.createElement("span");
    note.className = "muted small";
    note.textContent = `×${proposal.count}` + (proposal.overlaps_detected ? ` ${i18n.t("suggest.overlap")}` : "");

    row.append(check, select, value, note);
    list.append(row);
  }
}

/* I limiti del pannello: la finestra di caratteri che il modello riceve e il timeout della
   chiamata. I numeri arrivano da `/api/state`; qui c'e' solo il testo che li nomina. */
function updateSuggestLimits() {
  const element = $("suggest-limits");
  if (!element) return;
  const limits = state.suggestMaxChars && state.suggestTimeout
    ? i18n.t("suggest.limits", { max: state.suggestMaxChars, timeout: Math.round(state.suggestTimeout) })
    : "";
  // When the server covers a long document in chunks, the panel says so: otherwise the "first
  // {max} characters" sentence would read as "the rest is dropped" when it is not.
  const chunks = state.suggestChunkChars
    ? i18n.t("suggest.chunked", { chunk: state.suggestChunkChars, overlap: state.suggestChunkOverlap })
    : "";
  element.textContent = limits + chunks;
}

/** Quanti caratteri sono stati incollati, e quanti ne ricevera' il modello: il resto non parte. */
function updateSuggestCount() {
  const element = $("suggest-count");
  if (!element) return;
  const text = $("suggest-text").value;
  if (!text.length) {
    element.textContent = "";
    return;
  }
  const max = state.suggestMaxChars;
  element.textContent = max
    ? i18n.t("suggest.count", { n: text.length, max }) + (text.length > max ? i18n.t("suggest.countOver") : "")
    : String(text.length);
}

$("suggest-run").addEventListener("click", async () => {
  const button = $("suggest-run");
  const text = $("suggest-text").value;
  if (!text.trim()) {
    setStatus($("suggest-status"), i18n.t("suggest.paste"), "error");
    return;
  }
  button.disabled = true;
  // La chiamata al modello e' sincrona e puo' durare fino al timeout: la barra mostra i secondi
  // che passano invece di lasciare l'etichetta ferma, che si legge come "bloccato".
  setStatus($("suggest-status"), "", "");
  progressStart(i18n.t("suggest.reading"), PROGRESS_SUGGEST);
  progressWait(i18n.t("suggest.reading"), PROGRESS_SUGGEST);
  try {
    const live = $("suggest-live");
    live.textContent = "";
    live.hidden = false;
    const report = await suggestReport(
      { text, catalogs: selected(".catalog"), patterns: selected(".pattern") },
      (piece) => { live.textContent += piece; },
    );
    live.hidden = true; // il risultato sono le proposte; la bozza grezza era avanzamento
    renderSuggestions(report.candidates || []);
    const truncated = report.truncated
      ? i18n.t("suggest.truncated", { sent: report.analyzed_chars, total: report.chars })
      : "";
    setStatus($("suggest-status"), i18n.t("suggest.found", { n: (report.candidates || []).length }) + truncated, "ok");
  } catch (error) {
    // Un backend che non risponde e' un ERRORE, mai una lista vuota: la lista vuota si legge come
    // "niente da segnalare", che e' un'altra cosa. La bozza resta visibile: e' la prova di cosa
    // il modello ha detto davvero.
    renderSuggestions([]);
    setStatus($("suggest-status"), String(error.message || error), "error");
  } finally {
    progressStop(PROGRESS_SUGGEST);
    button.disabled = false;
  }
});

/* Una riga di dizionario e' `TIPO|valore`: un valore che contiene `|` o un a capo non e'
   rappresentabile e, scritto cosi', verrebbe letto come PIU' voci (o come una direttiva `@type`).
   Il valore resta nel testo e viene nominato, non aggiunto: scartarlo in silenzio sarebbe peggio. */
const dictionaryLine = (type, value) => (/[\n\r|]/.test(value) ? null : `${type}|${value}`);

$("suggest-add").addEventListener("click", () => {
  const chosen = [];
  const skipped = [];
  for (const row of $("suggest-list").querySelectorAll(".suggest-row")) {
    const check = row.querySelector("input[type=checkbox]");
    if (!check || !check.checked) continue;
    const proposal = state.suggestions[Number(check.dataset.index)];
    const type = row.querySelector("select").value;
    if (!proposal) continue;
    const line = dictionaryLine(type, proposal.value);
    if (line) chosen.push(line);
    else skipped.push(proposal.value);
  }
  if (!chosen.length && !skipped.length) {
    setStatus($("suggest-status"), i18n.t("suggest.none"), "error");
    return;
  }
  const area = $("entities-text");
  area.value = `${area.value.replace(/\s*$/, "")}\n${chosen.join("\n")}\n`;
  $("save-entities").disabled = false;
  renderSuggestions([]);
  const added = i18n.t("entities.added", { n: chosen.length });
  setStatus(
    $("suggest-status"),
    skipped.length ? added + i18n.t("entities.addedSkipped", { n: skipped.length }) : added,
    skipped.length ? "error" : "ok",
  );
});

$("save-entities").addEventListener("click", async () => {
  const button = $("save-entities");
  show(button, true, i18n.t("busy.saving"));
  try {
    const result = await request(`/api/entities?file=${encodeURIComponent(state.entitiesFile)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: $("entities-text").value }),
    });
    state.entitiesLoaded = $("entities-text").value;
    setStatus($("entities-status"), i18n.t("entities.saved", { n: result.entries }), "ok");
  } catch (error) {
    setStatus($("entities-status"), String(error.message || error), "error");
  } finally {
    show(button, false);
    refreshButtons();
  }
});
$("download-entities").addEventListener("click", () =>
  download(`${state.entitiesFile}.txt`, $("entities-text").value));

/* ---------------------------------------------------------------- inputs */
/**
 * ONE place that decides what a dropped/picked file means. The two paths used to disagree: the
 * picker populated `#file-anon` and the drop did not, so a dropped .docx armed nothing and the
 * button stayed disabled — the gesture simply did nothing, with no message saying why.
 */
async function acceptAnonFile(file) {
  if (isDocument(file.name)) {
    if (state.maxUploadBytes && file.size > state.maxUploadBytes) {
      state.anonFile = null;
      progressStop();
      setStatus(
        $("anon-status"),
        i18n.t("status.tooLarge", {
          name: file.name,
          size: humanSize(file.size),
          limit: humanSize(state.maxUploadBytes),
        }),
        "error",
      );
      return;
    }
    state.anonFile = file;
    $("text-anon").value = "";
    setStatus($("anon-status"), i18n.t("status.willConvert", { name: file.name, size: humanSize(file.size) }), "ok");
    return;
  }
  state.anonFile = null;
  $("file-anon").value = "";
  $("text-anon").value = await file.text();
  setStatus($("anon-status"), i18n.t("file.loaded", { name: file.name }), "ok");
}

/** Un drop con PIU' file apre una coda: ogni file ha la sua riga, la sua chiamata e i suoi
    artefatti. Un file solo resta il percorso classico (un testo riempie l'area, un documento arma
    il bottone), cosi' il gesto piu' comune non cambia. */
async function acceptAnonFiles(files) {
  const list = [...files];
  if (!list.length) return;
  if (list.length === 1 && !state.anonQueue.length) {
    await acceptAnonFile(list[0]);
    refreshButtons();
    return;
  }
  // Un'area di testo con dentro qualcosa verrebbe ignorata dalla coda: si svuota, cosi' l'unico
  // input armato e' la coda stessa e non resta testo che sembra in attesa di partire.
  $("text-anon").value = "";
  state.anonFile = null;
  $("file-anon").value = "";
  const refused = [];
  for (const file of list) {
    if (state.maxUploadBytes && file.size > state.maxUploadBytes) refused.push(file);
    else state.anonQueue.push({ file, status: "queued" });
  }
  renderQueue();
  if (refused.length && !state.anonQueue.length) {
    setStatus($("anon-status"), i18n.t("queue.refused", { n: refused.length }), "error");
  } else if (refused.length) {
    setStatus(
      $("anon-status"),
      i18n.t("queue.ready", { n: state.anonQueue.length }) + i18n.t("queue.refused", { n: refused.length }),
      "error",
    );
  } else {
    setStatus($("anon-status"), i18n.t("queue.ready", { n: state.anonQueue.length }), "ok");
  }
  refreshButtons();
}

dropzone($("drop-anon"), $("file-anon"), (files) => acceptAnonFiles(files));
dropzone($("drop-deanon"), $("file-deanon"), (files) => {
  state.deanonFile = files[0];
  setStatus($("deanon-status"), i18n.t("file.ready", { name: files[0].name }), "ok");
  refreshButtons();
});
dropzone($("drop-audit"), $("file-audit"), async (files) => {
  $("text-audit").value = await files[0].text();
  setStatus($("audit-status"), i18n.t("file.loaded", { name: files[0].name }), "ok");
  refreshButtons();
});

for (const id of ["text-anon", "text-audit"]) $(id).addEventListener("input", refreshButtons);
$("suggest-text").addEventListener("input", updateSuggestCount);
$("entities-text").addEventListener("input", refreshButtons);
document.querySelectorAll(".pattern").forEach((input) => input.addEventListener("change", optionsSummary));

/* The labels app.js composes are not in the markup: a language change has to redraw them. */
function renderRuntimeLabels() {
  const converter = $("converter-badge");
  if (!converter) return;
  converter.textContent = state.converter ? i18n.t("converter.badge.yes") : i18n.t("converter.badge.no");
  converter.title = state.converter ? i18n.t("converter.yes") : i18n.t("converter.no");
  $("suggest-state").textContent = state.suggest ? i18n.t("suggest.configured") : i18n.t("suggest.unconfigured");
  if (state.suggest) {
    $("suggest-backend").textContent = state.suggestBackend || i18n.t("suggest.localModel");
  }
  updateSuggestLimits();
  updateSuggestCount();
}
window.addEventListener("anon:lang-changed", () => {
  applyTheme(document.documentElement.dataset.theme || "dark");
  renderRuntimeLabels();
});
try {
  i18n.init();
} catch (error) {
  /* the interface stays Italian: a broken dictionary must not stop the tool */
}
applyTheme(document.documentElement.dataset.theme || "dark");

boot().catch((error) =>
  setStatus($("anon-status"), i18n.t("boot.failed", { detail: error.message || error }), "error"),
);
