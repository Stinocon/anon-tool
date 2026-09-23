#!/usr/bin/env node
/**
 * ui_load_check.mjs — load the real app.js against a minimal DOM that mirrors index.html.
 *
 * Why: a single `$("missing-id")` throws at load time and silently kills every initialisation
 * after it, leaving the UI half-dead. HTTP tests cannot see it (they never run the JS) and a
 * static grep only catches the id mistakes it is written for. This runs the actual script with
 * `getElementById` returning an element ONLY for ids declared in index.html — exactly the
 * browser's behaviour for this failure mode — so any load-time exception fails the gate.
 *
 * Usage: node tests/ui_load_check.mjs
 * Exit:  0 loaded clean, 1 it threw, 2 the check could not run.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.join(here, "..", "web");

let html;
try {
  html = readFileSync(path.join(webDir, "index.html"), "utf8");
  readFileSync(path.join(webDir, "app.js"), "utf8");
} catch (error) {
  console.error(`ui_load_check: cannot read the web files (${error.message})`);
  process.exit(2);
}

const declaredIds = new Set([...html.matchAll(/id="([^"]+)"/g)].map((match) => match[1]));
// Which elements the HTML itself closes. The stub starts from that state rather than a blanket
// `hidden: false`, so "inert before any script runs" becomes testable: without this, deleting the
// `hidden` attribute from a panel passed every check and only showed up as a flash of unarmed
// controls (or as armed controls when app.js never loads) in a browser.
const hiddenInHtml = new Set(
  [...html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*\shidden\b[^>]*>/g)].map((match) => match[1]),
);

/** A permissive element stub: every property the script touches must exist. */
function makeElement(tag = "div") {
  const element = {
    tagName: tag.toUpperCase(),
    children: [],
    style: {},
    dataset: {},
    hidden: hiddenInHtml.has(tag),
    value: "",
    checked: true,
    textContent: "",
    innerHTML: "",
    href: "",
    download: "",
    files: [],
    attributes: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    // Listeners are RECORDED, not discarded: without this the harness can only prove that app.js
    // parses, never that a gesture does what it says (the drag-and-drop bug this now covers was
    // invisible to every other check).
    listeners: {},
    addEventListener(type, handler) {
      (this.listeners[type] ||= []).push(handler);
    },
    removeEventListener() {},
    dispatchEvent(event) {
      for (const handler of this.listeners[event.type] || []) handler(event);
      return true;
    },
    append(...nodes) { this.children.push(...nodes); this.lastChild = nodes[nodes.length - 1]; },
    appendChild(node) { this.children.push(node); this.lastChild = node; return node; },
    click() {},
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) { return this.attributes[name] ?? null; },
    querySelectorAll: () => [],
    closest: () => null,
  };
  return element;
}

const elements = new Map();
globalThis.document = {
  // The real DOM always has documentElement: the theme code writes to it before first paint.
  documentElement: makeElement("html"),
  body: makeElement("body"),
  // ONE element per id, cached: `getElementById` returning a fresh stub each call would make every
  // post-gesture assertion vacuous (the handler mutates one object, the assertion reads another).
  getElementById: (id) => {
    if (!declaredIds.has(id)) return null;
    if (!elements.has(id)) elements.set(id, makeElement(id));
    return elements.get(id);
  },
  querySelectorAll: () => [],
  querySelector: () => null,
  createElement: (tag) => makeElement(tag),
  addEventListener() {},
};
globalThis.window = { ANON_TOKEN: "test-token", addEventListener() {} };
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
// `navigator` is a getter-only global in modern Node: redefine, do not assign.
Object.defineProperty(globalThis, "navigator", {
  value: { clipboard: { writeText: async () => {} } },
  configurable: true, writable: true,
});
globalThis.alert = () => {};
globalThis.URL.createObjectURL = () => "blob:stub";
globalThis.URL.revokeObjectURL = () => {};
globalThis.Blob = class Blob {};
globalThis.atob = (input) => Buffer.from(input, "base64").toString("binary");
const stubPayload = (url) => {
  if (String(url).includes("/api/maps")) return { maps: [] };
  if (String(url).includes("/api/entities")) return { text: "" };
  return {
    version: "test", schema: "anon/1", catalogs: [], patterns: ["identity"],
    maps_count: 0, converter: false, suggest: false, suggest_backend: null,
    entities_path: "/tmp/entities.txt",
    max_upload_bytes: 160 * 1024 * 1024,
  };
};
globalThis.fetch = async (url) => ({ ok: true, status: 200, json: async () => stubPayload(url) });

/** A file the browser would hand to a drop handler (only what the app actually reads). */
const makeFile = (name, size, text = "") => ({ name, size, text: async () => text });

const failures = [];
const check = (label, ok, detail) => {
  if (!ok) failures.push(`${label}${detail ? ` — ${detail}` : ""}`);
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${!ok && detail ? ` — ${detail}` : ""}`);
};

// Lo stato iniziale dell'HTML, asserito PRIMA che app.js giri: dopo il boot il JS sovrascrive
// `.hidden`, quindi un campo armato nel markup resterebbe invisibile a ogni altra asserzione e si
// vedrebbe solo come lampo di controlli attivi (o come controlli attivi, se lo script non carica).
check(
  "the HTML closes the panel's fields before any script runs",
  hiddenInHtml.has("suggest-fields") && hiddenInHtml.has("suggest-actions"),
  [...hiddenInHtml].join(" "),
);
const settle = () => new Promise((resolve) => setTimeout(resolve, 50));

try {
  await import(path.join(webDir, "app.js"));
  // Let the initial async chain (refresh → refreshMaps → loadEntities) settle.
  await settle();

  // --- a dropped DOCUMENT must arm the button (it did not: the drop never populated the input) ---
  const dropAnon = document.getElementById("drop-anon");
  const runAnon = document.getElementById("run-anon");
  dropAnon.dispatchEvent({
    type: "drop",
    preventDefault() {},
    dataTransfer: { files: [makeFile("verbale.docx", 2 * 1024 * 1024)] },
  });
  await settle();
  check("a dropped .docx enables Anonimizza", runAnon.disabled === false, `disabled=${runAnon.disabled}`);
  check(
    "the drop is reported in the status line",
    /verbale\.docx/.test(document.getElementById("anon-status").textContent),
    document.getElementById("anon-status").textContent,
  );

  // --- a file the server would refuse must be refused HERE, with the limit named ---
  dropAnon.dispatchEvent({
    type: "drop",
    preventDefault() {},
    dataTransfer: { files: [makeFile("enorme.docx", 500 * 1024 * 1024)] },
  });
  await settle();
  const oversizedStatus = document.getElementById("anon-status").textContent;
  check(
    "an oversized document is refused with the limit in the message",
    /160 MB/.test(oversizedStatus) && /500/.test(oversizedStatus),
    oversizedStatus,
  );

  // --- a dropped TEXT file fills the textarea and arms the button ---
  dropAnon.dispatchEvent({
    type: "drop",
    preventDefault() {},
    dataTransfer: { files: [makeFile("nota.txt", 40, "Cliente Contoso.\n")] },
  });
  await settle();
  check(
    "a dropped .txt fills the textarea and enables Anonimizza",
    /Contoso/.test(document.getElementById("text-anon").value) && runAnon.disabled === false,
  );

  // Il pannello si vede anche senza modello — dice come accenderlo — ma i suoi campi restano
  // chiusi, così non si può lanciare qualcosa che non esiste.
  check(
    "the local-model panel is visible and explains how to enable it",
    document.getElementById("suggest-card").hidden === false &&
      document.getElementById("suggest-off").hidden === false &&
      document.getElementById("suggest-fields").hidden === true,
  );
  // Il badge è lo stato che l'operatore legge: senza asserirlo, un pannello che dice sempre
  // "configurato" passerebbe tutti i controlli.
  check(
    "the badge reports the seam as unconfigured",
    document.getElementById("suggest-state").textContent === "non configurato",
  );

  if (failures.length) {
    console.error(`ui_load_check: ${failures.length} interaction check(s) failed`);
    process.exit(1);
  }
  console.log(`ui_load_check: app.js loaded clean against ${declaredIds.size} declared ids`);
  process.exit(0);
} catch (error) {
  console.error(`ui_load_check: app.js THREW at load — ${error && error.stack ? error.stack.split("\n").slice(0, 3).join("\n") : error}`);
  process.exit(1);
}
