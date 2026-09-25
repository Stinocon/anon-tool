/*
 * i18n.js — the interface language.
 *
 * Italian is the source: the text lives in index.html and is never moved here, so the page is
 * complete before any script runs, and a missing translation degrades to Italian instead of to an
 * empty label. This file carries the ENGLISH text, keyed by a CSS selector per element, and a few
 * dynamic strings that app.js composes (the language of those cannot come from the markup).
 *
 * Element keys, not phrase keys: word order differs between languages, and the paragraphs here mix
 * text with <strong>/<em>/<code>, so translating a fragment would produce English that does not
 * compose. One key per element replaces the whole inner HTML at once.
 *
 * The choice is persisted in localStorage (`anon-lang`, default `it`). The Italian inner HTML is
 * snapshotted at load, so switching back is a restore and not a second translation table.
 */
"use strict";

const EN = {
  "#lang-label": { inner: "Language" },
  "#run-anon": { inner: "Anonymize" },
  "#clear-anon": { inner: "Clear" },
  "#drop-audit-hint": { inner: "or paste the text below" },
  "#audit-reveal-label": { inner: "show the candidates" },
  "#audit-reveal-hint": { inner: "— they are the sensitive data" },
  "#entities-opt-generic": { inner: "entities.txt — generic" },
  "#entities-opt-people": { inner: "people.txt — people" },
  "#entities-opt-clients": { inner: "clients.txt — companies and offices" },
  "#badge-local": { inner: "local only", title: "The server listens on 127.0.0.1 only: no other device can reach it." },
  "#tab-anon": { inner: "Anonymize" },
  "#tab-deanon": { inner: "Restore" },
  "#tab-audit": { inner: "Check" },
  "#tab-entities": { inner: "Dictionary" },
  "#drop-anon-title": { inner: "Drop the document here" },
  "#drop-anon-hint": { inner: "doc · docx · pdf · txt · md · json · yaml · csv — up to" },
  "#drop-anon-hint-tail": { inner: ", or paste the text below" },
  "#opt-pattern-legend": { inner: "Patterns" },
  // Keyed on the TEXT spans, never on the <label>: replacing a label's inner HTML destroys the
  // <input> inside it — the pattern checkboxes disappeared in English, the summary then reported
  // "no patterns" while all of them still ran, and switching back re-checked boxes the user had
  // unticked. A translated element must never own an interactive child.
  "#opt-identity-name": { inner: "Identity" },
  "#opt-identity-hint": { inner: "email, URL, credentials" },
  "#opt-network-name": { inner: "Network" },
  "#opt-network-hint": { inner: "IPs, phones, hostnames" },
  "#opt-legal-name": { inner: "Legal" },
  "#opt-legal-hint": { inner: "tax code, VAT, IBAN, plates, addresses" },
  "#opt-catalogs-legend": { inner: 'Catalogs <span class="muted">— off: they redact heavily</span>' },
  "#catalogs-loading": { inner: "loading…" },
  "#entities-note": { inner: 'The <strong>custom dictionary</strong> is always active (the «Dictionary» tab).' },
  "#copy-redacted": { inner: "Copy the redacted text" },
  "#download-document": { inner: "Download the redacted document" },
  "#download-redacted": { inner: "Download the text (.md)" },
  "#download-report": { inner: "Download the report" },
  "#mapping-summary": { inner: "What became what" },
  "#highlight-summary": { inner: "Where it changed" },
  "#highlight-hint": {
    inner: "Every replaced value is a pill labelled with its type; no real value is shown here.",
  },
  "#mapping-warning": {
    inner:
      'This is the <strong>map</strong>: it shows the real values. Do not paste it into a chat with a model.',
  },
  "#reveal-map": { inner: "Show the real values" },
  "#hide-map": { inner: "Hide the values" },
  "#deanon-title": { inner: "Put the real values back" },
  "#deanon-intro": {
    inner:
      "Load the final document (the one with the placeholders) and choose the map it was redacted with.",
  },
  "#drop-deanon-title": { inner: "Drop the final document here" },
  "#deanon-map-title": { inner: "Map to use" },
  "#run-deanon": { inner: "Restore the real values" },
  "#audit-title": { inner: "Is it really anonymized?" },
  "#audit-intro": {
    inner:
      "A deterministic check <strong>before</strong> a model reads the document: leftover sensitive content, remaining placeholders, and dictionary names that were not redacted.",
  },
  "#drop-audit-title": { inner: "Drop the file to check here" },
  "#run-audit": { inner: "Check" },
  "#entities-title": { inner: "Custom dictionary" },
  "#entities-format": {
    inner:
      'One line per entity: <code>TIPO|value</code>, or just <code>value</code>. Aliases: <code>TIPO|value|alias</code>. Directives: <code>@type</code>, <code>@stem</code>, <code>@match case-sensitive</code>, <code>@context regex</code>',
  },
  "#entities-file-label": { inner: "File" },
  "#entities-file-prefix": { inner: "File:" },
  "#entities-path-tail": { inner: "— it stays on your machine, outside any repository." },
  "#save-entities": { inner: "Save" },
  "#download-entities": { inner: "Download the file" },
  "#suggest-title": { inner: "Suggestions from the local model" },
  "#suggest-off": {
    inner:
      'A local model can <em>propose</em> the strings worth redacting that the dictionary does not know. It is not configured: start it with <code>make model</code> (or <code>make up MODEL=1</code>), which downloads Qwen2.5-3B-Instruct and runs it beside the UI; by hand, start the server with <code>--suggest-url</code> and <code>--suggest-model</code> (see the README). The model runs in a container that shares the UI\'s network, so <code>127.0.0.1</code> is the same loopback for both; a model <em>on the host</em> stays unreachable from here.',
  },
  "#suggest-privacy": {
    inner:
      'The text you paste here goes to an endpoint <strong>on this machine</strong> (loopback): it does not leave it. The model <em>proposes</em> strings to redact; you approve them, and approving adds them to the dictionary above — the only place the engine redacts from. The panel writes nothing on its own.',
  },
  "#suggest-model-label": { inner: "Model:" },
  "#suggest-run": { inner: "Suggest" },
  "#suggest-hint": {
    inner:
      'A <em>reasoning</em> model can take tens of seconds, and may not answer at all: the error says so, and a mute backend is an error, never an empty list.',
  },
  "#suggest-add": { inner: "Add to the dictionary" },
  "#suggest-actions-hint": { inner: 'then press <strong>Save</strong> and re-run the anonymization' },
  "#footer-line": {
    inner:
      'Deterministic and local engine · the tool makes no network calls · the real values stay in <code>~/.anon/maps/</code>',
  },
  "#footer-links": {
    inner:
      '<a href="https://github.com/Stinocon/anon-tool" target="_blank" rel="noreferrer noopener">anon-tool</a>\n    <span aria-hidden="true">·</span>\n    <a href="https://github.com/Stinocon/pi-workbench" target="_blank" rel="noreferrer noopener">pi-workbench</a>\n    <span aria-hidden="true">·</span>\n    <span>Pi\'s <code>anon</code> skill: <code>/anon &lt;file&gt;</code></span>',
  },
};

/* Placeholders live on an attribute, not in the element tree. */
const EN_PLACEHOLDERS = {
  "#text-anon": "…paste the text to anonymize here",
  "#text-audit": "…paste the text to check here",
  "#suggest-text": "…paste the text the local model should read",
};

/* Strings app.js composes: they cannot come from the markup. `{name}` is substituted. */
const DYNAMIC = {
  it: {
    options: "Opzioni",
    "theme.dark": "Tema: scuro",
    "theme.light": "Tema: chiaro",
    "theme.toDark": "Passa al tema scuro",
    "theme.toLight": "Passa al tema chiaro",
    // Le etichette che app.js compone fuori dal markup: prima erano scritte in italiano nel codice,
    // quindi in inglese restavano italiane («Opzioni — tutti i pattern · nessun catalogo»).
    "busy.default": "Elaborazione…",
    "busy.saving": "Salvo…",
    "chips.none": "nessuna sostituzione",
    "summary.allPatterns": "tutti i pattern",
    "summary.patterns": "pattern: {list}",
    "summary.noPatterns": "nessun pattern",
    "summary.catalogs": "cataloghi: {list}",
    "summary.noCatalogs": "nessun catalogo",
    "catalogs.none": "nessun catalogo installato",
    "catalogs.entries": "{n} voci",
    "maps.entries": "{n} voci",
    "maps.none": "Nessuna mappa: anonimizza qualcosa nella prima scheda.",
    "mapping.notShown": "non ancora mostrata",
    "mapping.relocked": "valori reali rimossi dalla pagina",
    "mapping.relockedTimeout": "valori reali rimossi dalla pagina (tempo scaduto)",
    "deanon.downloaded": "fatto: file scaricato",
    "report.title": "Report di redazione",
    "report.file": "Documento",
    "report.tool": "Strumento",
    "report.date": "Data",
    "report.map": "Mappa",
    "report.counts": "Sostituzioni per tipo",
    "report.type": "Tipo",
    "report.count": "Numero",
    "report.placeholders": "Segnaposto",
    "report.note": "Nessun valore reale è incluso: i valori stanno in ~/.anon/maps/. Verifica questo report contro la mappa.",
    "deanon.incompleteDetail": "INCOMPLETO: vedi il dettaglio",
    "entities.added": "{n} aggiunte: premi Salva, poi rilancia l'anonimizzazione",
    "entities.addedSkipped": " — {n} non aggiunte (contengono «|» o un a capo)",
    "suggest.limits": "Il modello riceve al più {max} caratteri e la chiamata si interrompe dopo {timeout} s senza risposta.",
    "suggest.count": "{n} caratteri · al modello vanno i primi {max}",
    "suggest.countOver": " — oltre la finestra: il resto non viene inviato",
    "error.request": "richiesta fallita ({status})",
    "error.network": "caricamento interrotto (rete)",
    "audit.line": "riga {n}",
    "audit.verdict": "verdetto: {verdict}",
    "converter.badge.yes": "docx e pdf",
    "converter.badge.no": "solo testo",
    "converter.yes": "Il convertitore per docx/pdf è disponibile: puoi caricare anche quei formati.",
    "converter.no": "Nessun convertitore: carica solo txt, md, json, yaml, csv.",
    "suggest.configured": "configurato",
    "suggest.unconfigured": "non configurato",
    "suggest.localModel": "modello locale",
    "progress.converting": "conversione e anonimizzazione in corso…",
    "progress.processing": "elaborazione…",
    "progress.checking": "controllo…",
    "progress.downloading": "scarico del documento…",
    "status.copied": "copiato",
    "status.downloaded": "documento scaricato",
    "status.notRewritable": "documento non riscrivibile, redatto il solo testo ({detail})",
    "status.rulesApplied": "{n} regole applicate",
    "status.willConvert": "{name} — {size}, sarà convertito dal server",
    "status.tooLarge": "{name} — {size} supera il limite di {limit}: il server lo rifiuterebbe, quindi non viene caricato. Usa la CLI sul file, o spezzalo.",
    "deanon.replaced": "sostituzioni",
    "deanon.remaining": "placeholder rimasti",
    "deanon.unknown": "token non noti",
    "deanon.map": "mappa di",
    "deanon.outcome": "esito",
    "deanon.complete": "completo",
    "deanon.incomplete": "INCOMPLETO — non consegnabile",
    "audit.clean": "Pulito — nessun contenuto sensibile, nessuna variante sospetta",
    "audit.sensitive": "Sensibile — {n} elemento/i ancora in chiaro: non farlo leggere a un modello",
    "audit.suspect": "Sospetto — nessun residuo diretto, ma alcune parole somigliano a entità dichiarate",
    "audit.cappedLabel": "limite scansione",
    "audit.capped": "elenco parziale: la ricerca dei candidati si è fermata ai limiti (400 parole / 200 entità)",
    "audit.placeholders": "{n} presenti — coerente con un documento già redatto",
    "entities.unsaved": "Ci sono modifiche non salvate: cambiare file le perde. Continuare?",
    "entities.saved": "salvato — {n} entità attive",
    "entities.tooLong": "il server lo rifiuterebbe, quindi non viene caricato. Usa la CLI sul file, o spezzalo.",
    "suggest.paste": "incolla prima il testo",
    "suggest.reading": "il modello locale sta leggendo…",
    "suggest.overlap": "— già rilevato dal motore",
    "suggest.none": "nessuna proposta selezionata",
    "suggest.found": "{n} proposte",
    "suggest.truncated": " — inviati {sent} caratteri su {total}",
    "file.loaded": "{name} caricato",
    "file.ready": "{name} pronto",
    "boot.failed": "avvio fallito: {detail}",
    "download.failed": "scarico del documento fallito ({status})",
    "maps.broken": "{id} — mappa illeggibile (file corrotto o scrittura interrotta)",
    "progress.loading": "caricamento di {name} — {size}…",
    "filename.text": "testo",
    "result.title": "Risultato — {name}",
    "result.sameMap": "{name} — stesso tag e stessa mappa del testo qui sotto",
    "maps.truncated": "Mostrate le prime {shown} mappe su {total}. Le più vecchie si ripuliscono con `anon.py --prune-maps <giorni>`.",
  },
  en: {
    options: "Options",
    "theme.dark": "Theme: dark",
    "theme.light": "Theme: light",
    "theme.toDark": "Switch to the dark theme",
    "theme.toLight": "Switch to the light theme",
    "busy.default": "Working…",
    "busy.saving": "Saving…",
    "chips.none": "no replacement",
    "summary.allPatterns": "all patterns",
    "summary.patterns": "patterns: {list}",
    "summary.noPatterns": "no patterns",
    "summary.catalogs": "catalogs: {list}",
    "summary.noCatalogs": "no catalogs",
    "catalogs.none": "no catalog installed",
    "catalogs.entries": "{n} entries",
    "maps.entries": "{n} entries",
    "maps.none": "No map yet: anonymize something in the first tab.",
    "mapping.notShown": "not shown yet",
    "mapping.relocked": "real values removed from the page",
    "mapping.relockedTimeout": "real values removed from the page (timeout)",
    "deanon.downloaded": "done: file downloaded",
    "report.title": "Redaction report",
    "report.file": "Document",
    "report.tool": "Tool",
    "report.date": "Date",
    "report.map": "Map",
    "report.counts": "Substitutions by type",
    "report.type": "Type",
    "report.count": "Count",
    "report.placeholders": "Placeholders",
    "report.note": "No real value is included: the values stay in ~/.anon/maps/. Check this report against the map.",
    "deanon.incompleteDetail": "INCOMPLETE: see the detail",
    "entities.added": "{n} added: press Save, then re-run the anonymization",
    "entities.addedSkipped": " — {n} not added (they contain «|» or a newline)",
    "suggest.limits": "The model receives at most {max} characters, and the call gives up after {timeout} s without an answer.",
    "suggest.count": "{n} characters · the model gets the first {max}",
    "suggest.countOver": " — over the window: the rest is not sent",
    "error.request": "request failed ({status})",
    "error.network": "upload interrupted (network)",
    "audit.line": "line {n}",
    "audit.verdict": "verdict: {verdict}",
    "converter.badge.yes": "docx and pdf",
    "converter.badge.no": "text only",
    "converter.yes": "The docx/pdf converter is available: you can upload those formats too.",
    "converter.no": "No converter: upload txt, md, json, yaml, csv only.",
    "suggest.configured": "configured",
    "suggest.unconfigured": "not configured",
    "suggest.localModel": "local model",
    "progress.converting": "converting and anonymizing…",
    "progress.processing": "working…",
    "progress.checking": "checking…",
    "progress.downloading": "downloading the document…",
    "status.copied": "copied",
    "status.downloaded": "document downloaded",
    "status.notRewritable": "the document cannot be rewritten, only its text was redacted ({detail})",
    "status.rulesApplied": "{n} rules applied",
    "status.willConvert": "{name} — {size}, it will be converted by the server",
    "status.tooLarge": "{name} — {size} exceeds the {limit} limit: the server would refuse it, so it is not uploaded. Use the CLI on the file, or split it.",
    "deanon.replaced": "replacements",
    "deanon.remaining": "placeholders left",
    "deanon.unknown": "unknown tokens",
    "deanon.map": "map from",
    "deanon.outcome": "outcome",
    "deanon.complete": "complete",
    "deanon.incomplete": "INCOMPLETE — not deliverable",
    "audit.clean": "Clean — no sensitive content, no suspicious variant",
    "audit.sensitive": "Sensitive — {n} item(s) still in the clear: do not let a model read it",
    "audit.suspect": "Suspicious — no direct residual, but some words resemble declared entities",
    "audit.cappedLabel": "scan bound",
    "audit.capped": "partial list: the candidate search stopped at its bounds (400 words / 200 entities)",
    "audit.placeholders": "{n} present — consistent with an already redacted document",
    "entities.unsaved": "There are unsaved changes: switching file loses them. Continue?",
    "entities.saved": "saved — {n} active entries",
    "entities.tooLong": "the server would refuse it, so it is not uploaded. Use the CLI on the file, or split it.",
    "suggest.paste": "paste the text first",
    "suggest.reading": "the local model is reading…",
    "suggest.overlap": "— already found by the engine",
    "suggest.none": "no proposal selected",
    "suggest.found": "{n} proposals",
    "suggest.truncated": " — {sent} of {total} characters sent",
    "file.loaded": "{name} loaded",
    "file.ready": "{name} ready",
    "boot.failed": "startup failed: {detail}",
    "download.failed": "downloading the document failed ({status})",
    "maps.broken": "{id} — unreadable map (corrupt file or interrupted write)",
    "progress.loading": "loading {name} — {size}…",
    "filename.text": "text",
    "result.title": "Result — {name}",
    "result.sameMap": "{name} — same tag and same map as the text below",
    "maps.truncated": "Showing the first {shown} maps of {total}. The older ones are pruned with `anon.py --prune-maps <days>`.",
  },
};

const STORAGE_KEY = "anon-lang";
const LANGUAGES = ["it", "en"];
const italian = new Map(); // selector -> inner HTML / attribute value, captured once
let current = "it";

function snapshot() {
  for (const selector of Object.keys(EN)) {
    const element = document.querySelector(selector);
    if (!element) continue;
    italian.set(selector, element.innerHTML);
    if (EN[selector].title) italian.set(`${selector}@title`, element.getAttribute("title") || "");
  }
  for (const selector of Object.keys(EN_PLACEHOLDERS)) {
    const element = document.querySelector(selector);
    if (element) italian.set(`${selector}@placeholder`, element.getAttribute("placeholder") || "");
  }
  italian.set("document.title", document.title);
  const html = document.documentElement;
  if (html) italian.set("html@lang", html.getAttribute("lang") || "it");
}

function apply(language) {
  current = LANGUAGES.includes(language) ? language : "it";
  const english = current === "en";
  for (const selector of Object.keys(EN)) {
    const element = document.querySelector(selector);
    if (!element) continue;
    const value = english ? EN[selector].inner : italian.get(selector);
    if (value !== undefined) element.innerHTML = value;
    if (EN[selector].title) {
      const title = english ? EN[selector].title : italian.get(`${selector}@title`);
      if (title !== undefined) element.setAttribute("title", title);
    }
  }
  for (const selector of Object.keys(EN_PLACEHOLDERS)) {
    const element = document.querySelector(selector);
    if (!element) continue;
    const value = english ? EN_PLACEHOLDERS[selector] : italian.get(`${selector}@placeholder`);
    if (value !== undefined) element.setAttribute("placeholder", value);
  }
  document.title = english ? "anon-tool — local anonymization" : italian.get("document.title") || document.title;
  // `lang` on <html> is not decoration: it is what a screen reader uses to pick a voice.
  const root = document.documentElement;
  if (root) root.setAttribute("lang", english ? "en" : italian.get("html@lang") || "it");
  const select = document.getElementById("lang");
  if (select) select.value = current;
  try {
    localStorage.setItem(STORAGE_KEY, current);
  } catch (error) {
    /* a blocked localStorage must not break the page */
  }
  // The strings app.js composes are not in the markup, so a language change has to tell it to
  // redraw them. An event, not a direct call: this module must not know about app.js.
  if (typeof window !== "undefined" && typeof window.dispatchEvent === "function") {
    try {
      window.dispatchEvent(new Event("anon:lang-changed"));
    } catch (error) {
      /* an environment without Event (the test harness) simply does not redraw */
    }
  }
  return current;
}

/** The string for a key app.js composes. `{name}` placeholders are substituted. */
function t(key, params) {
  const table = DYNAMIC[current] || DYNAMIC.it;
  let value = table[key];
  if (value === undefined) value = DYNAMIC.it[key] !== undefined ? DYNAMIC.it[key] : key;
  if (params) {
    for (const [name, replacement] of Object.entries(params)) {
      value = value.replaceAll(`{${name}}`, String(replacement));
    }
  }
  return value;
}

function init() {
  let saved = "it";
  try {
    saved = localStorage.getItem(STORAGE_KEY) || "it";
  } catch (error) {
    saved = "it";
  }
  snapshot();
  const select = document.getElementById("lang");
  if (select) {
    select.addEventListener("change", () => apply(select.value));
  }
  return apply(saved);
}

window.AnonI18n = { init, apply, t, languages: LANGUAGES, get current() { return current; } };
