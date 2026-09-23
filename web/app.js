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

/* ---------------------------------------------------------------- tema */
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const button = $("theme-toggle");
  const light = theme === "light";
  button.textContent = light ? "Tema: chiaro" : "Tema: scuro";
  button.setAttribute("aria-pressed", String(light));
  button.title = light ? "Passa al tema scuro" : "Passa al tema chiaro";
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
  $("options-summary").textContent = `Opzioni — ${parts.join(" · ")}`;
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
  converter.textContent = info.converter ? "docx e pdf" : "solo testo";
  converter.className = info.converter ? "badge badge-ok" : "badge badge-warn";
  converter.title = info.converter
    ? "Il convertitore per docx/pdf è disponibile: puoi caricare anche quei formati."
    : "Nessun convertitore: carica solo txt, md, json, yaml, csv.";
  $("entities-path").textContent = info.entities_path;
  // The upload cap belongs to the server: the UI states it instead of keeping its own copy.
  state.maxUploadBytes = info.max_upload_bytes || null;
  if (state.maxUploadBytes) $("upload-limit").textContent = humanSize(state.maxUploadBytes);

  // Il seam verso un modello locale esiste solo se il server e' stato avviato con
  // --suggest-url/--suggest-model: senza modello il pannello non si mostra, invece di mostrarsi e
  // fallire al primo clic.
  state.suggest = Boolean(info.suggest);
  $("suggest-card").hidden = !state.suggest;
  if (state.suggest) $("suggest-backend").textContent = info.suggest_backend || "modello locale";

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
    note.textContent = `Mostrate le prime ${maps.length} mappe su ${total}. Le più vecchie si ripuliscono con ` +
      "`anon.py --prune-maps <giorni>`.";
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
  setStatus($("anon-status"), "elaborazione…");
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
          if (fraction >= 1) progressWait("conversione e anonimizzazione in corso…");
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
      setStatus($("anon-status"), `documento non riscrivibile, redatto il solo testo (${result.container_error})`, "warn");
    } else {
      setStatus($("anon-status"), `${result.rules_applied} regole applicate`, "ok");
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
  setStatus($("anon-status"), "copiato", "ok");
});
$("download-redacted").addEventListener("click", () => download(pending.name, pending.text));
$("download-document").addEventListener("click", async () => {
  if (!pending.containerUrl) return;
  setStatus($("anon-status"), "scarico del documento…");
  try {
    await downloadFromServer(pending.containerUrl, pending.containerName);
    setStatus($("anon-status"), "documento scaricato", "ok");
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
  setStatus($("deanon-status"), "elaborazione…");
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
      ["sostituzioni", String(report.replaced ?? 0), false],
      ["placeholder rimasti", String(report.remaining ?? 0), (report.remaining ?? 0) > 0],
      ["token non noti", String(report.unknown_placeholders ?? 0), (report.unknown_placeholders ?? 0) > 0],
      ["mappa di", report.map_source || "?", false],
      ["esito", report.complete ? "completo" : "INCOMPLETO — non consegnabile", !report.complete],
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
  setStatus($("audit-status"), "controllo…");
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
        ? "Pulito — nessun contenuto sensibile, nessuna variante sospetta"
        : result.verdict === "sensitive"
          ? `Sensibile — ${result.total} elemento/i ancora in chiaro: non farlo leggere a un modello`
          : "Sospetto — nessun residuo diretto, ma alcune parole somigliano a entità dichiarate";
    chips($("audit-types"), result.types);

    const near = $("audit-near");
    near.innerHTML = "";
    if (result.candidates_capped) {
      // The engine stops the near-miss search at a bound (400 words / 200 entities). Said out
      // loud: a short list would otherwise read as "nothing suspicious" when it is only partial.
      const row = document.createElement("div");
      row.className = "row bad";
      row.innerHTML = '<span class="k">limite scansione</span><span></span>';
      row.lastChild.textContent =
        "elenco parziale: la ricerca dei candidati si è fermata ai limiti (400 parole / 200 entità)";
      near.append(row);
    }
    if (result.placeholders_present) {
      const row = document.createElement("div");
      row.className = "row";
      row.innerHTML = `<span class="k">placeholder</span><span></span>`;
      row.lastChild.textContent =
        `${result.placeholders_present} presenti — coerente con un documento già redatto`;
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
    const go = confirm("Ci sono modifiche non salvate: cambiare file le perde. Continuare?");
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
    note.textContent = `×${proposal.count}` + (proposal.overlaps_detected ? " — già rilevato dal motore" : "");

    row.append(check, select, value, note);
    list.append(row);
  }
}

$("suggest-run").addEventListener("click", async () => {
  const button = $("suggest-run");
  const text = $("suggest-text").value;
  if (!text.trim()) {
    setStatus($("suggest-status"), "incolla prima il testo", "error");
    return;
  }
  button.disabled = true;
  setStatus($("suggest-status"), "il modello locale sta leggendo…", "");
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
    const truncated = report.truncated ? ` — inviati ${report.analyzed_chars} caratteri su ${report.chars}` : "";
    setStatus($("suggest-status"), `${(report.candidates || []).length} proposte${truncated}`, "ok");
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
    setStatus($("suggest-status"), "nessuna proposta selezionata", "error");
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
    setStatus($("entities-status"), `salvato — ${result.entries} entità attive`, "ok");
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
        `${file.name} — ${humanSize(file.size)} supera il limite di ${humanSize(state.maxUploadBytes)}: ` +
          "il server lo rifiuterebbe, quindi non viene caricato. Usa la CLI sul file, o spezzalo.",
        "error",
      );
      return;
    }
    state.anonFile = file;
    $("text-anon").value = "";
    setStatus($("anon-status"), `${file.name} — ${humanSize(file.size)}, sarà convertito dal server`, "ok");
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

boot().catch((error) => setStatus($("anon-status"), `avvio fallito: ${error.message || error}`, "error"));
