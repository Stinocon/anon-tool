/* anon-tool UI — vanilla, no dependencies, no CDN (works offline).
   The page only ever talks to its own origin, always with the per-run token header. */

const TOKEN = window.ANON_TOKEN;
const $ = (id) => document.getElementById(id);
const api = (path, options = {}) =>
  fetch(path, { ...options, headers: { "X-Anon-Token": TOKEN, ...(options.headers || {}) } });

const state = { maps: [], catalogs: [], redactedName: "redatto", lastMapId: null };

/* ---------------------------------------------------------------- tabs */
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => {
      const active = t === tab;
      t.classList.toggle("is-active", active);
      t.setAttribute("aria-selected", String(active));
      $(t.getAttribute("aria-controls")).classList.toggle("is-active", active);
    });
  });
});

/* ---------------------------------------------------------------- helpers */
function chips(container, counts) {
  const entries = Object.entries(counts || {});
  container.innerHTML = "";
  if (!entries.length) {
    const span = document.createElement("span");
    span.className = "chip chip-zero";
    span.textContent = "nessuna sostituzione";
    container.append(span);
    return;
  }
  for (const [type, count] of entries.sort()) {
    const span = document.createElement("span");
    span.className = "chip";
    span.textContent = `${type} × ${count}`;
    container.append(span);
  }
}

function download(name, text, binary) {
  const blob = binary
    ? new Blob([Uint8Array.from(atob(binary), (c) => c.charCodeAt(0))], { type: "application/octet-stream" })
    : new Blob([text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}

function setStatus(element, message, kind = "") {
  element.textContent = message;
  element.style.color = kind === "error" ? "var(--danger)" : kind === "ok" ? "var(--ok)" : "";
}

async function readFile(file) {
  return await file.text();
}

function dropzone(zone, input, onFile, acceptBinary = false) {
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

const selected = (selector) => [...document.querySelectorAll(selector)].filter((i) => i.checked).map((i) => i.value);

/* ---------------------------------------------------------------- state */
async function refresh() {
  const response = await api("/api/state");
  const info = await response.json();
  $("version").textContent = `v${info.version} · ${info.schema}`;
  const conv = $("converter-badge");
  conv.textContent = info.converter ? "converter: docx/pdf ok" : "converter: assente";
  conv.className = info.converter ? "badge badge-ok" : "badge";
  $("entities-path").textContent = info.entities_path;

  state.catalogs = info.catalogs || [];
  $("catalogs").innerHTML = "";
  if (!state.catalogs.length) {
    $("catalogs").innerHTML = '<span class="muted small">nessun catalogo installato</span>';
  }
  for (const catalog of state.catalogs) {
    const label = document.createElement("label");
    label.innerHTML = `<input type="checkbox" class="catalog" value="${catalog.name}"> ${catalog.name} <span class="muted">${catalog.entries} voci</span>`;
    $("catalogs").append(label);
  }
  state.maps = info.maps || [];
}

async function refreshMaps() {
  const response = await api("/api/maps");
  const { maps } = await response.json();
  state.maps = maps;
  const select = $("map-select");
  select.innerHTML = '<option value="">— seleziona —</option>';
  for (const map of maps) {
    const option = document.createElement("option");
    option.value = map.id;
    const date = (map.created || "").slice(0, 16).replace("T", " ");
    const summary = Object.entries(map.counts || {}).map(([k, v]) => `${k}:${v}`).join(" ");
    option.textContent = `${date} · ${map.source || "—"} · ${map.entries} voci · ${summary}`;
    select.append(option);
  }
}

/* ---------------------------------------------------------------- anonimizza */
let anonPayload = null;

function showAnonymize(result, name) {
  $("anon-result").hidden = false;
  chips($("anon-counts"), result.counts);
  $("redacted").value = result.redacted;
  state.lastMapId = result.map_id;
  anonPayload = { text: result.redacted, name };
  $("anon-map").textContent = result.map_id ? `mappa: ${result.map_id}` : "nessuna mappa (niente da redigere)";
  $("mapping").innerHTML = '<span class="muted small">premi «Mostra la mappa»</span>';
}

$("run-anon").addEventListener("click", async () => {
  const button = $("run-anon");
  const file = $("file-anon").files[0];
  const text = $("text-anon").value;
  if (!file && !text.trim()) {
    setStatus($("anon-status"), "serve un file o del testo", "error");
    return;
  }
  button.disabled = true;
  setStatus($("anon-status"), "elaborazione…");
  try {
    if (file && file.type !== "text/plain" && anonNeedsUpload(file.name)) {
      const response = await api("/api/anonymize-document", {
        method: "POST",
        headers: {
          "X-Filename": file.name,
          "X-Catalogs": selected(".catalog").join(","),
          "X-Patterns": selected(".pattern").join(","),
          "Content-Type": "application/octet-stream",
        },
        body: await file.arrayBuffer(),
      });
      const result = await response.json();
      if (result.error) throw new Error(result.error);
      showAnonymize(result, file.name.replace(/\.[^.]+$/, "") + ".redacted.md");
      setStatus($("anon-status"), `convertito e redatto (${result.origin})`, "ok");
    } else {
      const payload = { text: file ? await readFile(file) : text, catalogs: selected(".catalog"), patterns: selected(".pattern") };
      const response = await api("/api/anonymize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = await response.json();
      if (result.error) throw new Error(result.error);
      const base = file ? file.name : "incollato";
      showAnonymize(result, base.replace(/\.[^.]+$/, "") + ".redacted.txt");
      setStatus($("anon-status"), `fatto · ${result.rules_applied} regole applicate`, "ok");
    }
    await refreshMaps();
  } catch (error) {
    setStatus($("anon-status"), String(error.message || error), "error");
  } finally {
    button.disabled = false;
  }
});

function anonNeedsUpload(name) {
  return /\.(docx?|xlsx?|pptx?|odt|ods|odp|pdf|rtf|epub)$/i.test(name);
}

$("copy-redacted").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("redacted").value);
  setStatus($("anon-status"), "copiato", "ok");
});
$("download-redacted").addEventListener("click", () => {
  if (anonPayload) download(anonPayload.name, anonPayload.text);
});

$("reveal-map").addEventListener("click", async () => {
  if (!state.lastMapId) return;
  const response = await api("/api/maps/reveal", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: state.lastMapId, confirm: true }),
  });
  const data = await response.json();
  if (data.error) {
    $("mapping").textContent = data.error;
    return;
  }
  $("mapping").innerHTML = "";
  for (const [placeholder, info] of Object.entries(data.entries)) {
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML = `<span class="ph">${placeholder}</span><span class="muted">${info.type}</span><span></span>`;
    row.lastChild.textContent = info.original;
    $("mapping").append(row);
  }
});

/* ---------------------------------------------------------------- deanonimizza */
$("run-deanon").addEventListener("click", async () => {
  const file = $("file-deanon").files[0];
  const mapId = $("map-select").value;
  if (!file || !mapId) {
    setStatus($("deanon-status"), "servono documento e mappa", "error");
    return;
  }
  $("run-deanon").disabled = true;
  setStatus($("deanon-status"), "elaborazione…");
  try {
    const response = await api("/api/deanonymize", {
      method: "POST",
      headers: { "X-Filename": file.name, "X-Map-Id": mapId, "Content-Type": "application/octet-stream" },
      body: await file.arrayBuffer(),
    });
    const result = await response.json();
    if (result.error) throw new Error(result.error);
    const report = result.report || {};
    const box = $("deanon-report");
    box.hidden = false;
    box.innerHTML = "";
    const rows = [
      ["sostituzioni", String(report.replaced ?? 0)],
      ["residui", String(report.remaining ?? 0)],
      ["esito", report.complete ? "completo" : "INCOMPLETO — non consegnabile"],
    ];
    for (const [key, value] of rows) {
      const row = document.createElement("div");
      row.className = "row";
      row.innerHTML = `<span class="k">${key}</span><span></span>`;
      row.lastChild.textContent = value;
      if (key === "esito" && !report.complete) row.lastChild.style.color = "var(--danger)";
      box.append(row);
    }
    const suffix = file.name.replace(/\.[^.]+$/, "");
    const extension = (file.name.match(/\.[^.]+$/) || [".txt"])[0];
    download(`${suffix}-finale${extension}`, null, result.content_b64);
    setStatus($("deanon-status"), report.complete ? "fatto: file deanonimizzato scaricato" : "INCOMPLETO: vedi sopra", report.complete ? "ok" : "error");
  } catch (error) {
    setStatus($("deanon-status"), String(error.message || error), "error");
  } finally {
    $("run-deanon").disabled = false;
  }
});

/* ---------------------------------------------------------------- verifica */
async function runAudit() {
  const text = $("text-audit").value;
  if (!text.trim()) {
    setStatus($("anon-status"), "", "");
    alert("Incolla del testo o carica un file da verificare.");
    return;
  }
  const response = await api("/api/audit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      text,
      catalogs: selected(".catalog"),
      patterns: selected(".pattern"),
      reveal: $("audit-reveal").checked,
    }),
  });
  const result = await response.json();
  if (result.error) {
    alert(result.error);
    return;
  }
  $("audit-result").hidden = false;
  const verdict = $("audit-verdict");
  verdict.className = `verdict ${result.verdict}`;
  verdict.textContent =
    result.verdict === "clean"
      ? "PULITO — nessun contenuto sensibile, nessuna variante sospetta"
      : result.verdict === "sensitive"
        ? `SENSIBILE — ${result.total} elemento/i ancora in chiaro: NON farlo leggere a un modello`
        : "SOSPETTO — nessun residuo diretto, ma alcune parole somigliano a entità dichiarate";
  chips($("audit-types"), result.types);
  const near = $("audit-near");
  near.innerHTML = "";
  if (result.placeholders_present) {
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML = `<span class="k">placeholder</span><span></span>`;
    row.lastChild.textContent = `${result.placeholders_present} presenti (coerente con un documento già redatto)`;
    near.append(row);
  }
  for (const item of result.near_miss || []) {
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML = `<span class="k">riga ${item.line}</span><span class="muted">${item.kind} · ${item.type}</span><span></span>`;
    row.lastChild.textContent = item.token ? `${item.token} ~ ${item.entity}` : item.token_masked;
    near.append(row);
  }
}

$("run-audit").addEventListener("click", runAudit);
dropzone($("drop-audit"), $("file-audit"), async (file) => {
  $("text-audit").value = await readFile(file);
});
$("audit-reveal").addEventListener("change", () => {
  if ($("audit-result").hidden === false) runAudit();
});

/* ---------------------------------------------------------------- dizionario */
async function loadEntities() {
  const response = await api("/api/entities");
  const data = await response.json();
  $("entities-text").value = data.text || "";
}
$("save-entities").addEventListener("click", async () => {
  const response = await api("/api/entities", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: $("entities-text").value }),
  });
  const result = await response.json();
  if (result.error) {
    setStatus($("entities-status"), result.error, "error");
    return;
  }
  setStatus($("entities-status"), `salvato · ${result.entries} regole attive`, "ok");
  await refresh();
});
$("download-entities").addEventListener("click", () => {
  download("entities.txt", $("entities-text").value);
});

/* ---------------------------------------------------------------- init */
dropzone($("drop-anon"), $("file-anon"), async (file) => {
  if (anonNeedsUpload(file.name)) {
    setStatus($("anon-status"), `${file.name}: sarà convertito dal server`, "");
    $("text-anon").value = "";
  } else {
    $("text-anon").value = await readFile(file);
    setStatus($("anon-status"), `${file.name} caricato`, "ok");
  }
});
dropzone($("drop-deanon"), null, () => {});

refresh().then(() => {
  refreshMaps();
  loadEntities();
});
