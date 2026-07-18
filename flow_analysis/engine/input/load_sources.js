"use strict";
const fs = require("fs");
const path = require("path");
const SKIP_DIR_NAMES = new Set(["node_modules", "__MACOSX", ".git"]);
const SKIP_FILE_NAMES = new Set(["vendor.js", "vendors.js", "runtime.js", "manifest.js", "taro.js"]);
const MAX_FILE_BYTES = Number(process.env.PRIVGAP_MAX_JS_BYTES || 10 * 1024 * 1024);

function shouldSkip(relPath, fileName) {
  if (SKIP_FILE_NAMES.has(fileName)) return true;
  if (fileName.endsWith(".min.js")) return true;
  return relPath.split(path.sep).some(part => SKIP_DIR_NAMES.has(part));
}

function listJsFiles(appDir) {
  const out = [];
  function rec(dir) {
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch (_) {
      return;
    }
    for (const entry of entries) {
      if (entry.name.startsWith(".")) continue;
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (SKIP_DIR_NAMES.has(entry.name)) continue;
        rec(full);
      } else if (entry.isFile() && entry.name.endsWith(".js")) {
        out.push(full);
      }
    }
  }
  rec(appDir);
  return out;
}

function loadSourceFiles(appDir) {
  const files = [];
  for (const absPath of listJsFiles(appDir)) {
    const relPath = path.relative(appDir, absPath).split(path.sep).join("/");
    const fileName = path.basename(absPath);
    if (shouldSkip(relPath, fileName)) continue;
    let stat;
    try {
      stat = fs.statSync(absPath);
    } catch (_) {
      continue;
    }
    if (stat.size > MAX_FILE_BYTES || stat.size === 0) continue;
    let content;
    try {
      content = fs.readFileSync(absPath, "utf8");
    } catch (_) {
      continue;
    }
    files.push({ absPath, relPath, content });
  }
  return files;
}

module.exports = { loadSourceFiles };
