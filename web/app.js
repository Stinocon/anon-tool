/* anon-tool UI — vanilla, no dependencies, no CDN (works offline).
   One primary action per tab; everything secondary lives behind a <details>.
   The page only ever talks to its own origin, always with the per-run token header. */

const TOKEN = window.ANON_TOKEN;
const $ = (id) => document.getElementById(id);
const state = { maps: [], selectedMap: null, anonFile: null, deanonFile: null, entitiesFile: "entities", entitiesLoaded: "", lastMapId: null, maxUploadBytes: null, suggest: false, suggestions: [] };

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
  if (!response.ok) throw new Error(payload.error || `richiesta fallita (${response.status})`);
  return payload;
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

let progressTimer = null;
let progressShownAt = 0;
const PROGRESS_MIN_MS = 700; // a bar that blinks for 200ms is worse than none: keep it perceptible

function progressStart(label) {
  progressShownAt = Date.now();
  $("anon-progress").hidden = false;
  $("anon-progress").classList.remove("is-waiting");
  $("anon-progress-fill").style.width = "2%";
  $("anon-progress-fill").textContent = "";
  $("anon-progress-label").textContent = label;
}

function progressPercent(fraction) {
  const percent = Math.max(0, Math.min(100, Math.round(fraction * 100)));
  $("anon-progress-fill").style.width = `${percent}%`;
  $("anon-progress-fill").textContent = `${percent}%`;
}

/** The conversion and the anonymization happen inside ONE request, so there is no percentage to
    report while they run. The bar stops claiming one and shows elapsed time instead of a lie. */
function progressWait(label) {
  $("anon-progress").classList.add("is-waiting");
  const started = Date.now();
  const tick = () => {
    const seconds = Math.round((Date.now() - started) / 1000);
    $("anon-progress-label").textContent = `${label} — ${seconds}s`;
  };
  tick();
  clearInterval(progressTimer);
  progressTimer = setInterval(tick, 1000);
}

function progressStop() {
  clearInterval(progressTimer);
  progressTimer = null;
  const shown = Date.now() - progressShownAt;
  if (shown < PROGRESS_MIN_MS) {
    // Hide LATER, not now: the point is that the operator sees the phases, not that the element
    // is gone as fast as possible.
    setTimeout(progressStop, PROGRESS_MIN_MS - shown);
    return;
  }
  $("anon-progress").hidden = true;
  $("anon-progress").classList.remove("is-waiting");
  $("anon-progress-fill").style.width = "0%";
  $("anon-progress-fill").textContent = "";
  $("anon-progress-label").textContent = "";
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
      else reject(new Error(payload.error || `richiesta fallita (${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error("caricamento interrotto (rete)"));
    xhr.send(body);
  });
}

function show(button, busy, busyLabel = "Elaborazione…") {
  if (busy) {
    button.dataset.label = button.textContent;
    button.textContent = busyLabel;
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
    span.textContent = "nessuna sostituzione";
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
  if (!response.ok) throw new Error(`scarico del documento fallito (${response.status})`);
  saveBlob(name, await response.blob());
}

function dropzone(zone, input, onFile) {
  zone.addEventListener("dragover", (event) => {
    event.preventDefault();
    zone.classList.add("is-over");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("is-over"));
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    zone.classList.remove("is-over");
    if (event.dataTransfer.files.length) onFile(event.dataTransfer.files[0]);
  });
  if (input) input.addEventListener("change", () => input.files.length && onFile(input.files[0]));
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
 * i18n.js is loaded before this file. If it is missing (a stale cache, a partial deploy) the
 * interface must stay Italian and usable, not throw on the first t(): the fallback returns the
 * Italian string instead of the key, so a missing dictionary never shows "theme.dark" to a user. */
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
    patterns.length === 3 ? "tutti i pattern" : patterns.length ? `pattern: ${patterns.join(", ")}` : "nessun pattern",
    catalogs.length ? `cataloghi: ${catalogs.join(", ")}` : "nessun catalogo",
  ];
  $("options-summary").textContent = `${i18n.t("options")} — ${parts.join(" · ")}`;
}

function refreshButtons() {
  $("run-anon").disabled = !$("text-anon").value.trim() && !state.anonFile;
  $("clear-anon").hidden = !$("text-anon").value && !state.anonFile;
  $("run-deanon").disabled = !state.deanonFile || !state.selectedMap;
  $("run-audit").disabled = !$("text-audit").value.trim();
  $("save-entities").disabled = $("entities-text").value === state.entitiesLoaded;
}

async function boot() {
  const info = await request("/api/state");
  $("version").textContent = `v${info.version} · ${info.schema}`;
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

  state.converter = Boolean(info.converter);
  state.suggestBackend = info.suggest_backend || "";

  const catalogs = info.catalogs || [];
  $("catalogs").innerHTML = "";
  if (!catalogs.length) {
    $("catalogs").innerHTML = '<span class="muted small">nessun catalogo installato</span>';
  }
  for (const catalog of catalogs) {
    const label = document.createElement("label");
    label.innerHTML =
      `<input type="checkbox" class="catalog" value="${catalog.name}"> ` +
      `<span>${catalog.name}</span> <span class="muted">${catalog.entries} voci</span>`;
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
    list.innerHTML = '<p class="map-empty">Nessuna mappa: anonimizza qualcosa nella prima scheda.</p>';
    return;
  }
  for (const map of maps) {
    if (map.unreadable) {
      // A corrupt map is shown, not hidden: skipping it silently was why the list disagreed
      // with the total. It cannot be selected — restoring from it would fail anyway.
      const broken = document.createElement("p");
      broken.className = "map-empty";
      broken.textContent = `${map.id} — mappa illeggibile (file corrotto o scrittura interrotta)`;
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
      `${map.entries} voci · ${counts}${map.source ? ` · ${map.source}` : ""}</span></span>`;
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
$("run-anon").addEventListener("click", async () => {
  const button = $("run-anon");
  const file = state.anonFile;
  const text = $("text-anon").value;
  show(button, true);
  setStatus($("anon-status"), i18n.t("progress.processing"));
  try {
    let result;
    let baseName;
    if (file && isDocument(file.name)) {
      progressStart(`caricamento di ${file.name} — ${humanSize(file.size)}…`);
      result = await requestWithProgress(
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
      baseName = `${stripExtension(file.name)}.redacted.md`;
    } else {
      result = await request("/api/anonymize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: file ? await file.text() : text,
          catalogs: selected(".catalog"),
          patterns: selected(".pattern"),
        }),
      });
      baseName = `${file ? stripExtension(file.name) : "testo"}.redacted.txt`;
    }
    state.lastMapId = result.map_id;
    pending = {
      text: result.redacted,
      name: baseName,
      containerUrl: result.container_url || null,
      containerName: result.container_name || "",
    };

    $("anon-result").hidden = false;
    $("anon-result-title").textContent = `Risultato — ${pending.containerName || baseName}`;
    chips($("anon-counts"), result.counts);
    $("redacted").value = result.redacted;
    // The document itself, when the upload was one we can rewrite. One redaction produced both
    // artifacts and one map, so they can never disagree; the button is absent when there is
    // nothing to hand back (a PDF, a container we cannot open, or nothing to redact).
    $("download-document").hidden = !pending.containerUrl;
    $("download-document").title = pending.containerUrl
      ? `${pending.containerName} — stesso tag e stessa mappa del testo qui sotto`
      : "";
    if (result.container_error) {
      setStatus($("anon-status"), i18n.t("status.notRewritable", { detail: result.container_error }), "warn");
    } else {
      setStatus($("anon-status"), i18n.t("status.rulesApplied", { n: result.rules_applied }), "ok");
    }
    $("mapping").innerHTML = '<span class="muted small">non ancora mostrata</span>';
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

function relockMapping(message = "valori reali rimossi dalla pagina") {
  if (revealTimer) {
    clearTimeout(revealTimer);
    revealTimer = null;
  }
  $("mapping").innerHTML = `<span class="muted small">${message}</span>`;
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
    revealTimer = setTimeout(() => relockMapping("valori reali rimossi dalla pagina (tempo scaduto)"), REVEAL_TTL_MS);
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
      report.complete ? "fatto: file scaricato" : "INCOMPLETO: vedi il dettaglio",
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
      row.innerHTML = `<span class="k">riga ${item.line}</span><span class="muted">${item.kind} · ${item.type}</span><span></span>`;
      row.lastChild.textContent = item.token ? `${item.token} ~ ${item.entity}` : item.token_masked;
      near.append(row);
    }
    setStatus($("audit-status"), `verdetto: ${result.verdict}`, result.verdict === "clean" ? "ok" : "");
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

$("suggest-run").addEventListener("click", async () => {
  const button = $("suggest-run");
  const text = $("suggest-text").value;
  if (!text.trim()) {
    setStatus($("suggest-status"), i18n.t("suggest.paste"), "error");
    return;
  }
  button.disabled = true;
  setStatus($("suggest-status"), i18n.t("suggest.reading"), "");
  try {
    const report = await request("/api/suggest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        catalogs: selected(".catalog"),
        patterns: selected(".pattern"),
      }),
    });
    renderSuggestions(report.candidates || []);
    const truncated = report.truncated
      ? i18n.t("suggest.truncated", { sent: report.analyzed_chars, total: report.chars })
      : "";
    setStatus($("suggest-status"), i18n.t("suggest.found", { n: (report.candidates || []).length }) + truncated, "ok");
  } catch (error) {
    // Un backend che non risponde e' un ERRORE, mai una lista vuota: la lista vuota si legge come
    // "niente da segnalare", che e' un'altra cosa.
    renderSuggestions([]);
    setStatus($("suggest-status"), String(error.message || error), "error");
  } finally {
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
  const added = `${chosen.length} aggiunte: premi Salva, poi rilancia l'anonimizzazione`;
  setStatus(
    $("suggest-status"),
    skipped.length ? `${added} — ${skipped.length} non aggiunte (contengono «|» o un a capo)` : added,
    skipped.length ? "error" : "ok",
  );
});

$("save-entities").addEventListener("click", async () => {
  const button = $("save-entities");
  show(button, true, "Salvo…");
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
  setStatus($("anon-status"), `${file.name} caricato`, "ok");
}

dropzone($("drop-anon"), $("file-anon"), async (file) => {
  await acceptAnonFile(file);
  refreshButtons();
});
dropzone($("drop-deanon"), $("file-deanon"), (file) => {
  state.deanonFile = file;
  setStatus($("deanon-status"), `${file.name} pronto`, "ok");
  refreshButtons();
});
dropzone($("drop-audit"), $("file-audit"), async (file) => {
  $("text-audit").value = await file.text();
  setStatus($("audit-status"), `${file.name} caricato`, "ok");
  refreshButtons();
});

for (const id of ["text-anon", "text-audit"]) $(id).addEventListener("input", refreshButtons);
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

boot().catch((error) => setStatus($("anon-status"), `avvio fallito: ${error.message || error}`, "error"));
