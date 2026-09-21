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

/** A permissive element stub: every property the script touches must exist. */
function makeElement(tag = "div") {
  const element = {
    tagName: tag.toUpperCase(),
    children: [],
    style: {},
    dataset: {},
    hidden: false,
    value: "",
    checked: true,
    textContent: "",
    innerHTML: "",
    href: "",
    download: "",
    files: [],
    attributes: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {},
    removeEventListener() {},
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

globalThis.document = {
  // The real DOM always has documentElement: the theme code writes to it before first paint.
  documentElement: makeElement("html"),
  body: makeElement("body"),
  getElementById: (id) => (declaredIds.has(id) ? makeElement(id) : null),
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
    maps_count: 0, converter: false, entities_path: "/tmp/entities.txt",
  };
};
globalThis.fetch = async (url) => ({ ok: true, status: 200, json: async () => stubPayload(url) });

try {
  await import(path.join(webDir, "app.js"));
  // Let the initial async chain (refresh → refreshMaps → loadEntities) settle.
  await new Promise((resolve) => setTimeout(resolve, 50));
  console.log(`ui_load_check: app.js loaded clean against ${declaredIds.size} declared ids`);
  process.exit(0);
} catch (error) {
  console.error(`ui_load_check: app.js THREW at load — ${error && error.stack ? error.stack.split("\n").slice(0, 3).join("\n") : error}`);
  process.exit(1);
}
