"use strict";
const { analyzeForTaint, locOf, calleeApiString } = require("./dataflow");
const { propName, nearestEnclosingFunction } = require("../callgraph/resolve_callee");

const CONTINUATION_KEYS = new Set(["success", "complete", "then", "callback"]);

function isFunctionLike(node) {
  return node && (node.type === "FunctionExpression" || node.type === "ArrowFunctionExpression");
}

function methodNameFromCallee(callee) {
  if (!callee) return null;
  if (callee.type === "Identifier") return callee.name;
  if (callee.type === "MemberExpression") return propName(callee.property);
  if (callee.type === "SequenceExpression" && callee.expressions.length) {
    return methodNameFromCallee(callee.expressions[callee.expressions.length - 1]);
  }
  if (callee.type === "CallExpression") return methodNameFromCallee(callee.callee);
  return null;
}

function literalString(node) {
  if (!node) return null;
  if (node.type === "Literal" && (typeof node.value === "string" || typeof node.value === "number")) return String(node.value);
  if (node.type === "TemplateLiteral" && node.expressions.length === 0 && node.quasis.length === 1) {
    return node.quasis[0].value.cooked || node.quasis[0].value.raw || null;
  }
  return null;
}

function methodNameFromCall(callExpr) {
  if (!callExpr || callExpr.type !== "CallExpression") return null;
  if (callExpr.callee && callExpr.callee.type === "MemberExpression" &&
      propName(callExpr.callee.property) === "callProperty4Object") {
    return literalString((callExpr.arguments || [])[0]);
  }
  return methodNameFromCallee(callExpr.callee);
}

function storageSourceKey(callExpr) {
  const method = methodNameFromCall(callExpr);
  const args = (callExpr && callExpr.arguments) || [];
  if (method === "getStorageSync") return literalString(args[0]) || "*";
  if (method === "getStorage") {
    const arg = args[0];
    if (arg && arg.type === "ObjectExpression") {
      for (const prop of arg.properties || []) {
        if (!prop || prop.type !== "Property") continue;
        const key = propName(prop.key) || literalString(prop.key);
        if (key === "key") return literalString(prop.value) || "*";
      }
    }
    return "*";
  }
  return null;
}

function bindingPatternNames(pattern) {
  const names = new Set();
  function walk(pat) {
    if (!pat) return;
    if (pat.type === "Identifier") {
      names.add(pat.name);
      return;
    }
    if (pat.type === "AssignmentPattern") {
      walk(pat.left);
      return;
    }
    if (pat.type === "RestElement") {
      walk(pat.argument);
      return;
    }
    if (pat.type === "ObjectPattern") {
      for (const prop of pat.properties || []) {
        if (!prop) continue;
        walk(prop.type === "Property" ? (prop.value || prop.key) : prop.argument);
      }
      return;
    }
    if (pat.type === "ArrayPattern") {
      for (const elem of pat.elements || []) walk(elem);
    }
  }
  walk(pattern);
  return names;
}

function continuationSeedNames(params) {
  if (!params || !params.length) return null;
  const names = bindingPatternNames(params[0]);
  return names.size ? names : null;
}

function findSourceCalls(bodyNode, platformSpec) {
  const calls = [];
  function walk(n) {
    if (!n || typeof n !== "object") return;
    if (Array.isArray(n)) {
      for (const c of n) walk(c);
      return;
    }
    if (n.type === "CallExpression") {
      const method = methodNameFromCall(n);
      if (method && platformSpec.sourceMethods.includes(method)) calls.push(n);
    }
    for (const k of Object.keys(n)) {
      if (k === "type" || k === "loc" || k === "range") continue;
      walk(n[k]);
    }
  }
  walk(bodyNode);
  return calls;
}

function asyncCallbackContinuation(sourceCall) {
  for (const arg of sourceCall.arguments || []) {
    if (!arg || arg.type !== "ObjectExpression") continue;
    for (const prop of arg.properties || []) {
      const key = propName(prop.key);
      if (key && CONTINUATION_KEYS.has(key) && isFunctionLike(prop.value)) {
        const params = prop.value.params || [];
        const seedNames = continuationSeedNames(params);
        if (seedNames) {
          return { fnNode: prop.value, seedNames };
        }
      }
    }
  }
  return null;
}

function thenContinuation(sourceCall, parents) {
  const parent = parents.get(sourceCall);
  if (!parent || parent.type !== "MemberExpression" || propName(parent.property) !== "then") return null;
  const callExpr = parents.get(parent);
  if (!callExpr || callExpr.type !== "CallExpression") return null;
  const firstArg = (callExpr.arguments || [])[0];
  if (!isFunctionLike(firstArg)) return null;
  const params = firstArg.params || [];
  const seedNames = continuationSeedNames(params);
  if (!seedNames) return null;
  return { fnNode: firstArg, seedNames };
}

const ENTRY_CALL_NAMES = new Set(["Page", "Component", "App"]);

function findEntryObjectLiterals(bodyNode) {
  const objects = [];
  function walk(n) {
    if (!n || typeof n !== "object") return;
    if (Array.isArray(n)) {
      for (const c of n) walk(c);
      return;
    }
    if (n.type === "CallExpression" && n.callee.type === "Identifier" && ENTRY_CALL_NAMES.has(n.callee.name)) {
      const arg = (n.arguments || [])[0];
      if (arg && arg.type === "ObjectExpression") objects.push(arg);
    }
    for (const k of Object.keys(n)) {
      if (k === "type" || k === "loc" || k === "range") continue;
      walk(n[k]);
    }
  }
  walk(bodyNode);
  return objects;
}

function autoUiHandlerRegexes(platformSpec) {
  const patterns = (platformSpec && platformSpec.autoUiHandlerPatterns) || [];
  const out = [];
  for (const pattern of patterns) {
    try {
      out.push(new RegExp(pattern));
    } catch (_) {
      continue;
    }
  }
  return out;
}

function isAutoUiHandlerName(name, platformSpec) {
  if (!name) return false;
  return autoUiHandlerRegexes(platformSpec).some(re => re.test(name));
}

function objectMethodEntries(objLiteral) {
  const entries = [];
  if (!objLiteral || objLiteral.type !== "ObjectExpression") return entries;
  for (const prop of objLiteral.properties || []) {
    const name = propName(prop.key);
    if (!name) continue;
    if (isFunctionLike(prop.value)) {
      entries.push({ name, fnNode: prop.value });
      continue;
    }
    if (name === "methods" && prop.value && prop.value.type === "ObjectExpression") {
      for (const inner of prop.value.properties || []) {
        const innerName = propName(inner.key);
        if (innerName && isFunctionLike(inner.value)) entries.push({ name: innerName, fnNode: inner.value });
      }
    }
  }
  return entries;
}

function collectObjectLiterals(bodyNode) {
  const objects = [];
  function walk(n) {
    if (!n || typeof n !== "object") return;
    if (Array.isArray(n)) {
      for (const c of n) walk(c);
      return;
    }
    if (n.type === "ObjectExpression") objects.push(n);
    for (const k of Object.keys(n)) {
      if (k === "type" || k === "loc" || k === "range") continue;
      walk(n[k]);
    }
  }
  walk(bodyNode);
  return objects;
}

function functionLikeFromValue(value) {
  if (isFunctionLike(value)) return value;
  if (!value || value.type !== "CallExpression") return null;
  for (const arg of value.arguments || []) {
    if (isFunctionLike(arg)) return arg;
  }
  return null;
}

function eventDetailFieldRead(fnNode) {
  const eventParam = (fnNode.params || []).find(param => param && param.type === "Identifier");
  if (!eventParam) return false;
  const allowed = new Set(["encryptedData", "iv", "code", "phoneNumber", "purePhoneNumber"]);
  let found = false;
  function walk(node) {
    if (!node || found || typeof node !== "object") return;
    if (Array.isArray(node)) {
      for (const item of node) walk(item);
      return;
    }
    
    if (node.type === "MemberExpression") {
      const field = propName(node.property);
      const detail = node.object;
      if (field && allowed.has(field) && detail && detail.type === "MemberExpression" &&
          propName(detail.property) === "detail" && detail.object && detail.object.type === "Identifier" &&
          detail.object.name === eventParam.name) {
        found = true;
        return;
      }
    }
    for (const key of Object.keys(node)) {
      if (key === "type" || key === "loc" || key === "range") continue;
      walk(node[key]);
    }
  }
  walk(fnNode.body);
  return found;
}

function findPhoneAuthorizationHandlers(bodyNode, platformSpec) {
  const enabledMethods = new Set((platformSpec && platformSpec.uiEventSourceMethods) || []);
  if (!enabledMethods.has("getPhoneNumber")) return [];
  const found = [];
  const seen = new Set();
  for (const object of collectObjectLiterals(bodyNode)) {
    for (const prop of object.properties || []) {
      if (!prop || prop.type !== "Property") continue;
      const name = propName(prop.key);
      if (!/^(?:on)?get(?:Realtime)?PhoneNumber$/i.test(name || "")) continue;
      const fnNode = functionLikeFromValue(prop.value);
      if (!fnNode || !eventDetailFieldRead(fnNode)) continue;
      const key = String(locOf(fnNode));
      if (seen.has(key)) continue;
      seen.add(key);
      found.push({ name: "getPhoneNumber", fnNode });
    }
  }
  return found;
}

function findHandlerFunctions(bodyNode, handlerNames, platformSpec) {
  const found = [];
  const explicit = handlerNames || new Set();
  const seen = new Set();
  function maybeAdd(name, fnNode) {
    if (!name || !fnNode) return;
    if (!explicit.has(name) && !isAutoUiHandlerName(name, platformSpec)) return;
    const key = `${name}:${locOf(fnNode)}`;
    if (seen.has(key)) return;
    seen.add(key);
    found.push({ name, fnNode });
  }
  const objects = explicit.size ? collectObjectLiterals(bodyNode) : findEntryObjectLiterals(bodyNode);
  for (const objLiteral of objects) {
    for (const { name, fnNode } of objectMethodEntries(objLiteral)) maybeAdd(name, fnNode);
  }
  return found;
}

function runAnalysis(bodyNode, seedNames, sourceInfo, module, programModel, ctx) {
  analyzeForTaint(bodyNode, seedNames, sourceInfo, module, programModel, ctx);
  for (let round = 0; round < 3 && ctx.pendingFieldEntryPoints && ctx.pendingFieldEntryPoints.length; round++) {
    const pending = ctx.pendingFieldEntryPoints;
    ctx.pendingFieldEntryPoints = [];
    for (const entry of pending) {
      analyzeForTaint(entry.bodyNode, new Set(entry.seedNames || []), sourceInfo, entry.module || module, programModel, ctx);
    }
  }
}

function findUiWitnessesInModule(module, programModel, platformSpec, handlerNames) {
  const hasExplicitHandlers = handlerNames && handlerNames.size;
  const hasAutoHandlers = platformSpec && platformSpec.autoUiHandlerPatterns && platformSpec.autoUiHandlerPatterns.length;
  const phoneAuthorizationHandlers = findPhoneAuthorizationHandlers(module.bodyNode, platformSpec);
  if (!hasExplicitHandlers && !hasAutoHandlers && !phoneAuthorizationHandlers.length) return [];
  const witnesses = [];
  const handlerCandidates = [
    ...findHandlerFunctions(module.bodyNode, handlerNames, platformSpec),
    ...phoneAuthorizationHandlers,
  ];
  const seenHandlers = new Set();
  for (const { name, fnNode } of handlerCandidates) {
    const handlerKey = `${name}:${locOf(fnNode)}`;
    if (seenHandlers.has(handlerKey)) continue;
    seenHandlers.add(handlerKey);
    const params = fnNode.params || [];
    const seedNames = continuationSeedNames(params);
    if (!seedNames) continue;
    if (!fnNode.body || fnNode.body.type !== "BlockStatement") continue;
    const sourceInfo = {
      sourceFile: module.sourceFile,
      sourceApi: name,
      sourceLoc: locOf(fnNode),
      startpointType: "ui",
    };
    const ctx = { platformSpec, witnesses, visited: new Set(), maxVisits: 200 };
    runAnalysis(fnNode.body, new Set(seedNames), sourceInfo, module, programModel, ctx);
  }
  return dedupe(witnesses);
}

function witnessKey(w) {
  return [w.source_file, w.source_api, w.source_loc, w.sink_file, w.sink_api, w.sink_loc, w.flow_type].join("|");
}

function dedupe(witnesses) {
  const seen = new Set();
  const out = [];
  for (const w of witnesses) {
    const key = witnessKey(w);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(w);
  }
  return out;
}

function findWitnessesInModule(module, programModel, platformSpec, parents) {
  const witnesses = [];
  const sourceCalls = findSourceCalls(module.bodyNode, platformSpec);

  for (const sourceCall of sourceCalls) {
    const sourceApi = calleeApiString(sourceCall);
    const sourceLoc = locOf(sourceCall);
    const sourceInfo = {
      sourceFile: module.sourceFile,
      sourceApi,
      sourceLoc,
      startpointType: "API:callback",
      storageKey: storageSourceKey(sourceCall),
    };

    const asyncCont = asyncCallbackContinuation(sourceCall);
    if (asyncCont) {
      const ctx = { platformSpec, witnesses, visited: new Set(), maxVisits: 200 };
      runAnalysis(asyncCont.fnNode.body, new Set(asyncCont.seedNames), sourceInfo, module, programModel, ctx);
      continue;
    }

    const thenCont = thenContinuation(sourceCall, parents);
    if (thenCont) {
      sourceInfo.startpointType = "API:promise";
      const ctx = { platformSpec, witnesses, visited: new Set(), maxVisits: 200 };
      runAnalysis(thenCont.fnNode.body, new Set(thenCont.seedNames), sourceInfo, module, programModel, ctx);
      continue;
    }

    sourceInfo.startpointType = "API:sync";
    const enclosingFn = nearestEnclosingFunction(parents, sourceCall);
    const enclosingBody = enclosingFn
      ? (enclosingFn.body && enclosingFn.body.type === "BlockStatement" ? enclosingFn.body : null)
      : module.bodyNode;
    if (!enclosingBody) continue; 
    const ctx = { platformSpec, witnesses, visited: new Set(), maxVisits: 200, seedFromSourceCallsInBody: true, sourceCall };
    runAnalysis(enclosingBody, new Set(), sourceInfo, module, programModel, ctx);
  }
  return dedupe(witnesses);
}

module.exports = { findWitnessesInModule, findUiWitnessesInModule };
