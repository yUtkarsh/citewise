// Shared bits for the library, ingest and report pages: theme toggle and small helpers.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);

function storage(fn, fallback) { try { return fn(); } catch { return fallback; } }
// Control, private-use and replacement characters (a text layer the font could not decode),
// raw or written as \u escapes, show as ⟨?⟩ instead of vanishing.
const GARBLED = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-�]|\\u00[01][0-9a-fA-F]|\\u[eE][0-9a-fA-F]{3}/g;
function esc(s) {
  return String(s ?? "").replace(GARBLED, "⟨?⟩")
    .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function fmtInt(n) { return n == null ? "–" : Number(n).toLocaleString("en-US"); }
function fmtPct(x) { return x == null ? "–" : `${(x * 100).toFixed(1)}%`; }
function fmtUsd(x) { return x == null ? "–" : `$${Number(x).toFixed(2)}`; }
function fmtSecs(s) {
  if (s == null) return "–";
  return s < 90 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
}

async function getJSON(url, opts) {
  const res = await fetch(url, opts);
  let data = null;
  try { data = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) throw new Error((data && (data.detail || data.message)) || `${res.status} ${res.statusText}`);
  return data;
}

function currentTheme() {
  const t = document.documentElement.dataset.theme;
  if (t) return t;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function initTheme() {
  const saved = storage(() => localStorage.getItem("theme"), null);
  if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
  const btn = $("#theme-toggle");
  if (btn) btn.addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    storage(() => localStorage.setItem("theme", next));
    document.dispatchEvent(new Event("themechange"));
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => document.dispatchEvent(new Event("themechange")));
}

// Server-Sent Events over GET with automatic replay (the server numbers events).
function streamEvents(url, onEvent, onEnd) {
  const source = new EventSource(url);
  const names = ["stage", "log", "counts", "graph", "graph_status", "end"];
  for (const name of names) {
    source.addEventListener(name, (e) => {
      let data = {};
      try { data = JSON.parse(e.data); } catch { /* keep {} */ }
      if (e.lastEventId) streamEvents.lastId = Number(e.lastEventId);
      if (name === "end") { source.close(); onEnd && onEnd(data); return; }
      onEvent(name, data);
    });
  }
  return source;
}

document.addEventListener("DOMContentLoaded", initTheme);
