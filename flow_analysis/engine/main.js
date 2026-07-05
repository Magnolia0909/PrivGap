"use strict";
const { buildProgramModel } = require("./input/program_model");
const { buildParentMap } = require("./callgraph/resolve_callee");
const { findWitnessesInModule, findUiWitnessesInModule } = require("./taint/witness");
const { collectWxmlHandlersByJsFile } = require("./input/wxml_sources");

function analyzeApp(appDir, platformSpec) {
  const programModel = buildProgramModel(appDir);
  const wxmlHandlersByJsFile = collectWxmlHandlersByJsFile(appDir);
  const allWitnesses = [];
  const seen = new Set();

  for (const module of programModel.modules) {
    if (!module.bodyNode) continue;
    const parents = buildParentMap(module.bodyNode);
    const witnesses = [
      ...findWitnessesInModule(module, programModel, platformSpec, parents),
      ...findUiWitnessesInModule(module, programModel, platformSpec, wxmlHandlersByJsFile.get(module.sourceFile)),
    ];
    for (const w of witnesses) {
      const key = [w.source_file, w.source_api, w.source_loc, w.sink_file, w.sink_api, w.sink_loc, w.flow_type].join("|");
      if (seen.has(key)) continue;
      seen.add(key);
      allWitnesses.push(w);
    }
  }
  return allWitnesses;
}

module.exports = { analyzeApp };
