"use strict";

const { buildCfg } = require("../cfg/build_cfg");

const {
  buildParentMap,
  isFunctionNode,
  propName,
  resolveCallee,
  nearestEnclosingFunction,
  buildScopeChain,
  findFunctionByName,
  allSiblingMethods,
} = require("../callgraph/resolve_callee");

const cfgCache = new Map();

function getCfg(bodyNode) {
  let cfg = cfgCache.get(bodyNode);

  if (!cfg) {
    cfg = buildCfg(bodyNode);
    cfgCache.set(bodyNode, cfg);
  }
  return cfg;
}

const parentsCache = new Map();

function getModuleParents(moduleBodyNode) {
  let parents = parentsCache.get(moduleBodyNode);

  if (!parents) {
    parents = buildParentMap(moduleBodyNode);
    parentsCache.set(moduleBodyNode, parents);
  }
  return parents;
}

function isSourceSemanticAPI(method, platformSpec) {
  return !!(
    method &&
    platformSpec &&
    platformSpec.sourceMethods &&
    platformSpec.sourceMethods.includes(method)
  );
}

const SENSITIVE_FIELD_HINT_RE =
  /^(phone|mobile|tel|user|userinfo|userInfo|avatar|headimg|nickname|name|realname|idcard|idCard|card|address|location|lat|lng|latitude|longitude|file|path|tempFilePath|tempFilePaths|code|token|openid|unionid|session_key)$/;

function isSensitiveFieldHint(name) {
  return !!name && SENSITIVE_FIELD_HINT_RE.test(name);
}

function isSinkSemanticAPI(method, platformSpec) {
  return !!(
    method &&
    platformSpec &&
    platformSpec.sinkMethods &&
    platformSpec.sinkMethods.includes(method)
  );
}

function sentTaintName(node) {
  if (
    node &&
    node.type === "MemberExpression" &&
    !node.computed &&
    node.property.type === "Identifier" &&
    node.property.name === "sent" &&
    node.object.type === "Identifier"
  ) {
    return `$sent:${node.object.name}`;
  }
  return null;
}

function methodNameFromCallee(callee) {
  if (!callee) return null;

  if (callee.type === "Identifier") {
    return callee.name;
  }
  if (callee.type === "MemberExpression") {
    return propName(callee.property);
  }
  if (
    callee.type === "SequenceExpression" &&
    callee.expressions.length
  ) {
    return methodNameFromCallee(
      callee.expressions[callee.expressions.length - 1]
    );
  }
  if (callee.type === "CallExpression") {
    return methodNameFromCallee(callee.callee);
  }
  return null;
}

function appxHelperName(callExpr) {
  if (!callExpr || callExpr.type !== "CallExpression") {
    return null;
  }
  if (
    !callExpr.callee ||
    callExpr.callee.type !== "MemberExpression"
  ) {
    return null;
  }
  return propName(callExpr.callee.property);
}

function isPlatformDispatchCall(callExpr) {
  return appxHelperName(callExpr) === "callProperty4Object";
}

function methodNameFromCall(callExpr) {
  if (!callExpr || callExpr.type !== "CallExpression") {
    return null;
  }
  if (isPlatformDispatchCall(callExpr)) {
    return literalString((callExpr.arguments || [])[0]);
  }
  return methodNameFromCallee(callExpr.callee);
}

function appxCallObject(callExpr) {
  return isPlatformDispatchCall(callExpr)
    ? (callExpr.arguments || [])[1]
    : null;
}

function appxCallArgumentArray(callExpr) {
  if (!isPlatformDispatchCall(callExpr)) {
    return null;
  }
  const argArray = (callExpr.arguments || [])[2];
  return argArray &&
    argArray.type === "ArrayExpression"
    ? (argArray.elements || []).filter(Boolean)
    : [];
}

function isSourceMethodName(method, platformSpec) {
  return isSourceSemanticAPI(method, platformSpec);
}

function memberTaintName(node) {
  if (!node || node.type !== "MemberExpression") {
    return null;
  }
  const prop = propName(node.property);
  if (!prop) {
    return null;
  }
  return `$field:${prop}`;
}

function literalString(node) {
  if (!node) {
    return null;
  }
  if (
    node.type === "Literal" &&
    (typeof node.value === "string" ||
      typeof node.value === "number")
  ) {
    return String(node.value);
  }
  if (
    node.type === "TemplateLiteral" &&
    node.expressions.length === 0 &&
    node.quasis.length === 1
  ) {
    return (
      node.quasis[0].value.cooked ||
      node.quasis[0].value.raw ||
      null
    );
  }
  return null;
}

function pathPartsFromMember(node) {
  if (!node || node.type !== "MemberExpression") {
    return null;
  }
  const parts = [];

  function push(n) {
    if (!n) {
      return false;
    }
    if (n.type === "ThisExpression") {
      parts.push("this");
      return true;
    }
    if (n.type === "Identifier") {
      parts.push(n.name);
      return true;
    }
    if (n.type === "MemberExpression") {
      if (!push(n.object)) {
        return false;
      }
      const p =
        propName(n.property) ||
        literalString(n.property);
      if (!p) {
        return false;
      }
      parts.push(String(p));
      return true;
    }
    if (
      n.type === "CallExpression" &&
      n.callee.type === "Identifier" &&
      n.callee.name === "getApp"
    ) {
      parts.push("getApp()");
      return true;
    }
    return false;
  }
  return push(node) ? parts : null;
}

function fieldNamesFromPathParts(parts) {
  const names = [];

  if (!parts || !parts.length) {
    return names;
  }
  const dataIdx = parts.lastIndexOf("data");
  if (
    dataIdx >= 0 &&
    dataIdx + 1 < parts.length
  ) {
    names.push(`$field:${parts[dataIdx + 1]}`);
  }
  const globalIdx = parts.lastIndexOf("globalData");
  if (
    globalIdx >= 0 &&
    globalIdx + 1 < parts.length
  ) {
    names.push(`$field:${parts[globalIdx + 1]}`);
  }
  const last = parts[parts.length - 1];
  if (isSensitiveFieldHint(last)) {
    names.push(`$field:${last}`);
  }
  return names;
}

function memberTaintNames(node) {
  const names = new Set();
  const fieldName = memberTaintName(node);
  if (fieldName) {
    names.add(fieldName);
  }
  const parts = pathPartsFromMember(node);
  if (parts && parts.length) {
    names.add(`$path:${parts.join(".")}`);

    for (const field of fieldNamesFromPathParts(parts)) {
      names.add(field);
    }
  }
  return names;
}

function syntheticMember(objectExpr, propertyName) {
  return {
    type: "MemberExpression",
    object: objectExpr,
    property: {
      type: "Identifier",
      name: propertyName,
    },
    computed: false,
  };
}

function appxGetPropertyTaintNames(callExpr) {
  const names = new Set();
  if (appxHelperName(callExpr) !== "getProperty4Object") {
    return names;
  }
  const args = callExpr.arguments || [];
  const prop = literalString(args[0]);
  const obj = args[1];
  if (!prop || !obj) {
    return names;
  }
  for (
    const name of memberTaintNames(
      syntheticMember(obj, String(prop))
    )
  ) {
    names.add(name);
  }
  return names;
}

function setDataAssignmentsFromCall(callExpr) {
  const steps = [];
  if (!callExpr || callExpr.type !== "CallExpression") {
    return steps;
  }
  if (
    !callExpr.callee ||
    callExpr.callee.type !== "MemberExpression"
  ) {
    return steps;
  }
  if (
    propName(callExpr.callee.property) !== "setData"
  ) {
    return steps;
  }
  const arg = (callExpr.arguments || [])[0];
  if (!arg || arg.type !== "ObjectExpression") {
    return steps;
  }
  for (const prop of arg.properties || []) {
    if (!prop || prop.type !== "Property") {
      continue;
    }
    const key =
      propName(prop.key) ||
      literalString(prop.key);
    if (!key || !prop.value) {
      continue;
    }
    const keyParts =
      String(key).split(".").filter(Boolean);
    if (!keyParts.length) {
      continue;
    }
    const names = new Set([
      `$path:this.data.${keyParts.join(".")}`,
    ]);
    const first = keyParts[0];
    const last = keyParts[keyParts.length - 1];
    if (isSensitiveFieldHint(first)) {
      names.add(`$field:${first}`);
    }
    if (isSensitiveFieldHint(last)) {
      names.add(`$field:${last}`);
    }
    for (const name of names) {
      steps.push({
        name,
        rhs: prop.value,
      });
    }
  }
  return steps;
}

function bindingPatternSteps(pattern, rhs) {
  const steps = [];
  function walk(pat, valueExpr) {
    if (!pat) {
      return;
    }
    if (pat.type === "Identifier") {
      if (valueExpr) {
        steps.push({
          name: pat.name,
          rhs: valueExpr,
        });
      }
      return;
    }
    if (pat.type === "AssignmentPattern") {
      walk(
        pat.left,
        valueExpr || pat.right
      );
      return;
    }
    if (pat.type === "RestElement") {
      walk(
        pat.argument,
        valueExpr
      );
      return;
    }
    if (pat.type === "ObjectPattern") {
      for (const prop of pat.properties || []) {
        if (!prop) {
          continue;
        }
        if (prop.type === "RestElement") {
          walk(
            prop.argument,
            valueExpr
          );
          continue;
        }
        if (prop.type !== "Property") {
          continue;
        }
        const key =
          propName(prop.key) ||
          literalString(prop.key);
        const nextValue =
          key && valueExpr
            ? syntheticMember(
                valueExpr,
                String(key)
              )
            : valueExpr;
        walk(
          prop.value || prop.key,
          nextValue
        );
      }
      return;
    }
    if (pat.type === "ArrayPattern") {
      for (const elem of pat.elements || []) {
        walk(elem, valueExpr);
      }
    }
  }
  walk(pattern, rhs);
  return steps;
}

function bindingPatternNames(pattern) {
  const names = new Set();
  function walk(pat) {
    if (!pat) {
      return;
    }
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
        if (!prop) {
          continue;
        }
        walk(
          prop.type === "Property"
            ? (prop.value || prop.key)
            : prop.argument
        );
      }
      return;
    }
    if (pat.type === "ArrayPattern") {
      for (const elem of pat.elements || []) {
        walk(elem);
      }
    }
  }
  walk(pattern);
  return names;
}

function sourceMatches(node, sourceCall) {
  return !sourceCall || node === sourceCall;
}

function storageGetKey(callExpr) {
  if (!callExpr || callExpr.type !== "CallExpression") {
    return null;
  }
  if (
    !callExpr.callee ||
    callExpr.callee.type !== "MemberExpression"
  ) {
    return null;
  }
  const method =propName(callExpr.callee.property);
  if (
    method !== "getStorageSync" &&
    method !== "getStorage"
  ) {
    return null;
  }
  const args = callExpr.arguments || [];
  if (method === "getStorageSync") {
    return literalString(args[0]) || "*";
  }
  const keyVals =
    objectPropValues(
      args[0],
      ["key"]
    );
  return literalString(keyVals[0]) || "*";
}

function storageSetKey(callExpr) {
  if (!callExpr || callExpr.type !== "CallExpression") {
    return null;
  }
  if (
    !callExpr.callee ||
    callExpr.callee.type !== "MemberExpression"
  ) {
    return null;
  }
  const method = propName(callExpr.callee.property);
  const args = callExpr.arguments || [];
  if (method === "setStorageSync") {
    return literalString(args[0]) || "*";
  }
  if (method === "setStorage") {
    const keyVals =objectPropValues(args[0],["key"]);
    return literalString(keyVals[0]) || "*";
  }
  return null;
}

function isDirectSourceCall(node, platformSpec, sourceCall) {
  if (!node || node.type !== "CallExpression") {
    return false;
  }
  if (!sourceMatches(node, sourceCall)) {
    return false;
  }
  const method = methodNameFromCall(node);
  return isSourceSemanticAPI(method, platformSpec);
}

function containsSourceCall(node, platformSpec, sourceCall) {
  if (!node || !platformSpec) {
    return false;
  }
  let found = false;
  function walk(n) {
    if (found || !n ||typeof n !== "object"){
      return;
    }
    if (Array.isArray(n)) {
      for (const c of n) {
        walk(c);
      }
      return;
    }
    if (
      isDirectSourceCall(n, platformSpec, sourceCall)
    ) {
      found = true;
      return;
    }
    if (
      n.type === "FunctionExpression" ||
      n.type === "FunctionDeclaration" ||
      n.type === "ArrowFunctionExpression"
    ) {
      return;
    }
    for (const k of Object.keys(n)) {
      if (k === "type" || k === "loc" || k === "range"){
        continue;
      }
      walk(n[k]);
    }
  }
  walk(node);
  return found;
}

function isSourceValueExpression(node, platformSpec, sourceCall) {
  if (!node || !platformSpec) {
    return false;
  }
  if (
    isDirectSourceCall(node, platformSpec, sourceCall)
  ){
    return true;
  }
  if (node.type === "LogicalExpression") {
    return (
      isSourceValueExpression(node.left, platformSpec, sourceCall) ||
      isSourceValueExpression(node.right, platformSpec, sourceCall));
  }

  if (node.type === "ConditionalExpression") {
    return (
      isSourceValueExpression(node.consequent, platformSpec, sourceCall) ||
      isSourceValueExpression(node.alternate, platformSpec, sourceCall));
  }

  if (node.type === "SequenceExpression") {
    const items = node.expressions || [];
    return items.length ? isSourceValueExpression(items[items.length - 1], platformSpec, sourceCall) : false;
  }
  return false;
}