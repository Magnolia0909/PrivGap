"use strict";
const path = require("path");
const esprima = require("esprima");
const { loadSourceFiles } = require("./load_sources");
const { findModuleRegistries, moduleWrapperParams } = require("./bundling");

function parseFile(content) {
  try {
    return esprima.parseScript(content, { loc: true, range: true, tolerant: true });
  } catch (_) {
    try {
      return esprima.parseModule(content, { loc: true, range: true, tolerant: true });
    } catch (_) {
      return null;
    }
  }
}

function stripExt(p) {
  return p.replace(/\.js$/, "");
}

function resolveRelativeRequire(fromRelPath, requireArg, fileModulesByRelPath) {
  if (!requireArg.startsWith(".")) return null; 
  const fromDir = path.posix.dirname(fromRelPath);
  const joined = path.posix.normalize(path.posix.join(fromDir, requireArg));
  const candidates = [joined, `${stripExt(joined)}.js`, `${joined}.js`, `${joined}/index.js`];
  for (const cand of candidates) {
    if (fileModulesByRelPath.has(cand)) return fileModulesByRelPath.get(cand);
  }
  return null;
}

function buildProgramModel(appDir) {
  const files = loadSourceFiles(appDir);
  const modules = [];
  const fileModulesByRelPath = new Map();
  const webpackModulesByKey = new Map();

  for (const file of files) {
    const ast = parseFile(file.content);
    if (!ast) continue;

    const registries = findModuleRegistries(ast);
    if (registries.length > 0) {
      
      for (const registry of registries) {
        for (const { key, fnNode } of registry.keys) {
          const wrapperParams = moduleWrapperParams(fnNode);
          const mod = {
            moduleId: `webpack:${key}`,
            sourceFile: file.relPath,
            kind: "webpack",
            bodyNode: fnNode.body,
            fnNode,
            ...wrapperParams,
          };
          modules.push(mod);
          if (!webpackModulesByKey.has(key)) webpackModulesByKey.set(key, mod);
        }
      }
    }
    const fileMod = {
      moduleId: `file:${file.relPath}`,
      sourceFile: file.relPath,
      kind: "file",
      bodyNode: ast,
      fnNode: null,
      moduleParam: null,
      exportsParam: null,
      requireParam: null,
    };
    modules.push(fileMod);
    fileModulesByRelPath.set(file.relPath, fileMod);
  }

  return {
    appDir,
    modules,
    fileModulesByRelPath,
    webpackModulesByKey,
    appModule: fileModulesByRelPath.get("app.js") || fileModulesByRelPath.get("App.js") || null,
    resolveRequire(fromModule, requireArgNode) {
      if (!requireArgNode || requireArgNode.type !== "Literal") return null;
      const value = requireArgNode.value;
      if (fromModule.kind === "webpack") {
        const target = webpackModulesByKey.get(String(value));
        return target || null;
      }
      if (typeof value === "string") {
        return resolveRelativeRequire(fromModule.sourceFile, value, fileModulesByRelPath);
      }
      return null;
    },
  };
}

module.exports = { buildProgramModel, parseFile };
