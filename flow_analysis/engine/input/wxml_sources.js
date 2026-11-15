"use strict";
const fs = require("fs");
const path = require("path");

const TEMPLATE_EXTS = new Set([".wxml", ".axml", ".ttml"]);
const HANDLER_ATTR_RE = /\b((?:bind|catch|mut-bind|capture-bind|capture-catch|on)[A-Za-z0-9:_-]*)\s*=\s*["']([^"']+)["']/g;
const PHONE_OPEN_TYPE_RE = /\bopen-type\s*=\s*["'](?:getPhoneNumber|getRealtimePhoneNumber)["']/;
const PRIVACY_EVENT_RE = /(getphonenumber|getrealtimephonenumber|getuserinfo|chooseavatar|contact|submit|input|blur|confirm)$/i;
const PRIVACY_NAME_RE = /(phone|mobile|tel|user.?info|userinfo|auth|login|location|address|id.?card|identity|real.?name|nameinput|avatar|chooseavatar|getphonenumber|getuserinfo)/i;
const FORM_SOURCE_RE = /^(formSubmit|submitForm|submit)$/i;
const NON_PRIVACY_NAME_RE = /(pay|date|time|picker|gift|hobby|group|type|tab|radio|poster|agree|cancel|close|show|hide|popup|modal|city)$/i;
const TAG_PRIVACY_RE = /(phone|mobile|tel|用户|手机号|手机号码|身份证|证件|姓名|真实姓名|地址|位置|详细地址|avatar|头像|userinfo|user-info)/i;

function listTemplateFiles(appDir) {
  const out = [];
  function rec(dir) {
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch (_) {
      return;
    }
    for (const entry of entries) {
      if (entry.name.startsWith(".") || entry.name === "node_modules") continue;
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        rec(full);
      } else if (entry.isFile() && TEMPLATE_EXTS.has(path.extname(entry.name))) {
        out.push(full);
      }
    }
  }
  rec(appDir);
  return out;
}

function jsRelPathForTemplate(appDir, templatePath) {
  const rel = path.relative(appDir, templatePath).split(path.sep).join("/");
  return rel.replace(/\.(wxml|axml|ttml)$/i, ".js");
}

function isPrivacyHandler(attrName, handlerName, text, offset) {
  const attr = String(attrName || "").toLowerCase();
  const name = String(handlerName || "").trim();
  if (!name || name === "true" || name === "false" || name.startsWith("{{")) return false;
  if (attr.includes("submit")) return true;
  if (PRIVACY_NAME_RE.test(name)) return true;
  if (NON_PRIVACY_NAME_RE.test(name)) return false;
  if (FORM_SOURCE_RE.test(name)) {
    const start = Math.max(0, offset - 360);
    const end = Math.min(text.length, offset + 360);
    return TAG_PRIVACY_RE.test(text.slice(start, end));
  }
  if (!PRIVACY_EVENT_RE.test(attr)) return false;
  const start = Math.max(0, offset - 260);
  const end = Math.min(text.length, offset + 260);
  return TAG_PRIVACY_RE.test(text.slice(start, end));
}

function collectWxmlHandlersByJsFile(appDir) {
  const byJsFile = new Map();
  for (const file of listTemplateFiles(appDir)) {
    let text = "";
    try {
      text = fs.readFileSync(file, "utf8");
    } catch (_) {
      continue;
    }
    const handlers = new Set();
    let match;
    while ((match = HANDLER_ATTR_RE.exec(text)) !== null) {
      const attrName = String(match[1] || "").trim();
      const name = String(match[2] || "").trim();
      if (isPrivacyHandler(attrName, name, text, match.index)) handlers.add(name);
    }
    if (PHONE_OPEN_TYPE_RE.test(text)) {
      handlers.add("getPhoneNumber");
      handlers.add("getRealtimePhoneNumber");
    }
    if (!handlers.size) continue;
    const jsRelPath = jsRelPathForTemplate(appDir, file);
    if (!byJsFile.has(jsRelPath)) byJsFile.set(jsRelPath, new Set());
    const target = byJsFile.get(jsRelPath);
    for (const handler of handlers) target.add(handler);
  }
  return byJsFile;
}

module.exports = { collectWxmlHandlersByJsFile };
