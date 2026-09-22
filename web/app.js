/* anon-tool UI — vanilla, no dependencies, no CDN (works offline).
   One primary action per tab; everything secondary lives behind a <details>.
   The page only ever talks to its own origin, always with the per-run token header. */

const TOKEN = window.ANON_TOKEN;
const $ = (id) => document.getElementById(id);
const state = { maps: [], selectedMap: null, deanonFile: null, entitiesLoaded: "", lastMapId: null };

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

function download(name, text, base64) {
  const blob = base64
    ? new Blob([Uint8Array.from(atob(base64), (c) => c.charCodeAt(0))], { type: "application/octet-stream" })
    : new Blob([text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
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
  $("run-anon").disabled = !$("text-anon").value.trim() && !$("file-anon").files.length;
  $("clear-anon").hidden = !$("text-anon").value && !$("file-anon").files.length;
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
  const entities = await request("/api/entities");
  $("entities-text").value = entities.text || "";
  state.entitiesLoaded = $("entities-text").value;
  refreshButtons();
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
  const file = $("file-anon").files[0];
  const text = $("text-anon").value;
  show(button, true);
  setStatus($("anon-status"), "elaborazione…");
  try {
    let result;
    let baseName;
    if (file && isDocument(file.name)) {
      result = await request("/api/anonymize-document", {
        method: "POST",
        headers: {
          "X-Filename": file.name,
          "X-Catalogs": selected(".catalog").join(","),
          "X-Patterns": selected(".pattern").join(","),
          "Content-Type": "application/octet-stream",
        },
        body: await file.arrayBuffer(),
      });
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
    pending = { text: result.redacted, name: baseName };

    $("anon-result").hidden = false;
    $("anon-result-title").textContent = `Risultato — ${baseName}`;
    chips($("anon-counts"), result.counts);
    $("redacted").value = result.redacted;
    $("anon-map").textContent = result.map_id ? `mappa ${result.map_id} salvata` : "niente da redigere";
    $("mapping").innerHTML = '<span class="muted small">non ancora mostrata</span>';
    setStatus($("anon-status"), `${result.rules_applied} regole applicate`, "ok");
    $("anon-result").scrollIntoView({ block: "nearest" });
    await loadMaps();
  } catch (error) {
    setStatus($("anon-status"), String(error.message || error), "error");
  } finally {
    show(button, false);
    refreshButtons();
  }
});

let pending = { text: "", name: "redatto.txt" };

$("clear-anon").addEventListener("click", () => {
  $("text-anon").value = "";
  $("file-anon").value = "";
  $("anon-result").hidden = true;
  setStatus($("anon-status"), "");
  refreshButtons();
});
$("copy-redacted").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("redacted").value);
  setStatus($("anon-status"), "copiato", "ok");
});
$("download-redacted").addEventListener("click", () => download(pending.name, pending.text));

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
$("save-entities").addEventListener("click", async () => {
  const button = $("save-entities");
  show(button, true, "Salvo…");
  try {
    const result = await request("/api/entities", {
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
  download("entities.txt", $("entities-text").value));

/* ---------------------------------------------------------------- inputs */
dropzone($("drop-anon"), $("file-anon"), async (file) => {
  if (isDocument(file.name)) {
    $("text-anon").value = "";
    setStatus($("anon-status"), `${file.name} — sarà convertito dal server`);
  } else {
    $("text-anon").value = await file.text();
    $("file-anon").value = "";
    setStatus($("anon-status"), `${file.name} caricato`, "ok");
  }
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
