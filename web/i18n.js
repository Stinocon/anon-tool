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
  "#badge-local": { inner: "local only", title: "The server listens on 127.0.0.1 only: no other device can reach it." },
  "#tab-anon": { inner: "Anonymize" },
  "#tab-deanon": { inner: "Restore" },
  "#tab-audit": { inner: "Check" },
  "#tab-entities": { inner: "Dictionary" },
  "#drop-anon-title": { inner: "Drop the document here" },
  "#drop-anon-hint": {
    inner:
      'doc · docx · pdf · txt · md · json · yaml · csv — up to <span id="upload-limit">160 MB</span>, or paste the text below',
  },
  "#opt-pattern-legend": { inner: "Patterns" },
  "#opt-identity": { inner: '<span>Identity</span> <span class="muted">email, URL, credentials</span>' },
  "#opt-network": { inner: '<span>Network</span> <span class="muted">IPs, phones, hostnames</span>' },
  "#opt-legal": { inner: '<span>Legal</span> <span class="muted">tax code, VAT, IBAN, plates, addresses</span>' },
  "#opt-catalogs-legend": { inner: 'Catalogs <span class="muted">— off: they redact heavily</span>' },
  "#catalogs-loading": { inner: "loading…" },
  "#entities-note": { inner: 'The <strong>custom dictionary</strong> is always active (the «Dictionary» tab).' },
  "#copy-redacted": { inner: "Copy the redacted text" },
  "#download-document": { inner: "Download the redacted document" },
  "#download-redacted": { inner: "Download the text (.md)" },
  "#mapping-summary": { inner: "What became what" },
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
  "#entities-path-note": {
    inner:
      'File: <code id="entities-path">~/.anon/entities.txt</code> — it stays on your machine, outside any repository.',
  },
  "#save-entities": { inner: "Save" },
  "#download-entities": { inner: "Download the file" },
  "#suggest-title": { inner: "Suggestions from the local model" },
  "#suggest-off": {
    inner:
      'A local model can <em>propose</em> the strings worth redacting that the dictionary does not know. It is not configured: start the server with <code>--suggest-url</code> and <code>--suggest-model</code> (see the README). Inside the container this panel cannot work — there <code>127.0.0.1</code> is the container itself and the loopback rule refuses the host — so it is for the native server.',
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

/* Strings app.js composes: they cannot come from the markup. */
const EN_DYNAMIC = {
  options: "Options",
  "theme.dark": "Theme: dark",
  "theme.light": "Theme: light",
  "theme.toLight": "Switch to the light theme",
  "theme.toDark": "Switch to the dark theme",
};

const IT_DYNAMIC = {
  options: "Opzioni",
  "theme.dark": "Tema: scuro",
  "theme.light": "Tema: chiaro",
  "theme.toLight": "Passa al tema chiaro",
  "theme.toDark": "Passa al tema scuro",
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
  return current;
}

/** The string for a key app.js composes (options summary, theme button). */
function t(key) {
  const table = current === "en" ? EN_DYNAMIC : IT_DYNAMIC;
  return table[key] !== undefined ? table[key] : key;
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
