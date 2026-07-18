"use strict";

const fs = require("fs");
const { analyzeApp } = require("./main");

const [appDir, platform, specPath] = process.argv.slice(2);
if (!appDir || !platform || !specPath) {
  console.error("usage: node run_app.js <appDir> <platform> <specJsonPath>");
  process.exit(1);
}

const spec = JSON.parse(fs.readFileSync(specPath, "utf8"));
const platformSpec = {
  apiPrefix: spec.apiPrefix,
  sourceApis: spec.sourceApis,
  sourceMethods: spec.sourceMethods,
  sinkMethods: spec.sinkMethods,
  coreSinkMethods: spec.coreSinkMethods || spec.sinkMethods,
  flowTypeByMethod: spec.flowTypeByMethod,
  sinkPayloadKeys: spec.sinkPayloadKeys || {},
  suppressedFlowPatterns: spec.suppressedFlowPatterns || [],
  semanticSinkPatterns: spec.semanticSinkPatterns || [],
  resourceSinkPatterns: spec.resourceSinkPatterns || [],
  autoUiHandlerPatterns: spec.autoUiHandlerPatterns || [],
  uiEventSourceMethods: spec.uiEventSourceMethods || [],
  storageKeySensitiveLocalStorage: !!spec.storageKeySensitiveLocalStorage,
  wrapperTerminalMode: spec.wrapperTerminalMode || "official",
  directFileUploadArgs: Boolean(spec.directFileUploadArgs),
};

try {
  const witnesses = analyzeApp(appDir, platformSpec);
  process.stdout.write(JSON.stringify(witnesses));
} catch (err) {
  console.error(String((err && err.stack) || err));
  process.exit(1);
}
