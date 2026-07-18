"use strict";

function isFunctionLike(node) {
  return node && (node.type === "FunctionExpression" || node.type === "ArrowFunctionExpression");
}

function propKeyLiteral(prop) {
  const key = prop.key;
  if (!key) return null;
  if (key.type === "Literal") return key.value;
  if (key.type === "Identifier" && !prop.computed) return key.name;
  return null;
}

function looksLikeModuleKey(key) {
  if (/^[0-9]+$/.test(key)) return true; 
  if (/^[0-9a-f]{4,40}$/i.test(key)) return true; 
  if (key.includes("/") || key.startsWith(".")) return true; 
  return false;
}

function looksLikeModuleRegistry(node) {
  if (!node || node.type !== "ObjectExpression") return false;
  const props = node.properties || [];
  if (props.length < 2) return false;
  for (const prop of props) {
    if (prop.type !== "Property") return false;
    if (!isFunctionLike(prop.value)) return false;
    const key = propKeyLiteral(prop);
    if (key === null || !looksLikeModuleKey(String(key))) return false;
  }
  return true;
}

function findModuleRegistries(ast) {
  const registries = [];
  function walk(node) {
    if (!node || typeof node !== "object") return;
    if (Array.isArray(node)) {
      for (const child of node) walk(child);
      return;
    }
    if (node.type === "ObjectExpression" && looksLikeModuleRegistry(node)) {
      const keys = [];
      for (const prop of node.properties) {
        const key = propKeyLiteral(prop);
        if (key !== null && isFunctionLike(prop.value)) {
          keys.push({ key: String(key), fnNode: prop.value });
        }
      }
      registries.push({ keys, node });
      
    }
    for (const k of Object.keys(node)) {
      if (k === "type" || k === "loc" || k === "range") continue;
      walk(node[k]);
    }
  }
  walk(ast);
  return registries;
}

function moduleWrapperParams(fnNode) {
  const params = fnNode.params || [];
  const nameOf = p => (p && p.type === "Identifier" ? p.name : null);
  return {
    moduleParam: nameOf(params[0]),
    exportsParam: nameOf(params[1]),
    requireParam: nameOf(params[2]),
  };
}

module.exports = { findModuleRegistries, moduleWrapperParams, isFunctionLike };
