"use strict";

let nextNodeId = 0;
function freshId() {
  return `n${nextNodeId++}`;
}

function regeneratorBaseName(discriminant) {
  function baseOfNextAccess(node) {
    if (node && node.type === "MemberExpression" && !node.computed &&
        node.property.type === "Identifier" && node.property.name === "next" &&
        node.object.type === "Identifier") {
      return node.object.name;
    }
    return null;
  }
  if (discriminant.type === "AssignmentExpression") {
    return baseOfNextAccess(discriminant.right) || baseOfNextAccess(discriminant.left);
  }
  return baseOfNextAccess(discriminant);
}

function regeneratorYieldInfo(returnStmt, baseName) {
  const arg = returnStmt.argument;
  if (!arg) return null;
  const elements = arg.type === "SequenceExpression" ? arg.expressions : [arg];
  let nextCaseValue = null;
  let yieldedExpr = null;
  for (const el of elements) {
    if (el.type === "AssignmentExpression" && el.left.type === "MemberExpression" &&
        el.left.object.type === "Identifier" && el.left.object.name === baseName &&
        el.left.property.type === "Identifier" && el.left.property.name === "next" &&
        el.right.type === "Literal") {
      nextCaseValue = el.right.value;
    } else if (el.type !== "AssignmentExpression") {
      yieldedExpr = el; 
    }
  }
  return nextCaseValue === null ? null : { nextCaseValue, yieldedExpr };
}

class CfgBuilder {
  constructor() {
    this.nodes = new Map(); 
    this.exitNodes = new Set(); 
  }

  addNode(stmt) {
    const id = freshId();
    this.nodes.set(id, { stmt, succs: new Set(), preds: new Set(), isReturn: false, isThrow: false });
    return id;
  }

  addEdge(fromId, toId) {
    if (!fromId || !toId) return;
    this.nodes.get(fromId).succs.add(toId);
    this.nodes.get(toId).preds.add(fromId);
  }

  markExit(id) {
    this.exitNodes.add(id);
  }
}

function buildCfg(body) {
  const b = new CfgBuilder();
  const statements = body.body || [];
   
  function buildStmtList(stmts, loopCtx) {
    let entry = null;
    let prevExits = [];
    let lastEntry = null; 
    for (const stmt of stmts) {
      const { entry: stmtEntry, exits: stmtExits } = buildStmt(stmt, loopCtx);
      if (stmtEntry === null) continue; 
      if (entry === null) entry = stmtEntry;
      for (const exit of prevExits) b.addEdge(exit, stmtEntry);
      prevExits = stmtExits;
      lastEntry = stmtEntry;
    }
    return { entry, exits: prevExits, lastEntry };
  }

  function buildStmt(stmt, loopCtx) {
    switch (stmt.type) {
      case "BlockStatement":
        return buildStmtList(stmt.body, loopCtx);

      case "IfStatement": {
        const testId = b.addNode(stmt); 
        const cons = buildStmt(stmt.consequent, loopCtx);
        b.addEdge(testId, cons.entry || testId);
        let exits = cons.entry ? cons.exits : [testId];
        if (stmt.alternate) {
          const alt = buildStmt(stmt.alternate, loopCtx);
          b.addEdge(testId, alt.entry || testId);
          exits = exits.concat(alt.entry ? alt.exits : [testId]);
        } else {
          exits.push(testId); 
        }
        return { entry: testId, exits };
      }

      case "ForStatement":
      case "ForInStatement":
      case "ForOfStatement": {
        const headId = b.addNode(stmt); 
        const innerLoopCtx = { breakTargets: [], continueTargets: [headId] };
        const bodyRes = buildStmt(stmt.body, innerLoopCtx);
        b.addEdge(headId, bodyRes.entry || headId);
        for (const exit of bodyRes.entry ? bodyRes.exits : [headId]) b.addEdge(exit, headId);
        for (const cont of innerLoopCtx.continueTargets) {
          if (cont !== headId) b.addEdge(cont, headId);
        }
        const exits = [headId, ...innerLoopCtx.breakTargets];
        return { entry: headId, exits };
      }

      case "WhileStatement":
      case "DoWhileStatement": {
        const headId = b.addNode(stmt);
        const innerLoopCtx = { breakTargets: [], continueTargets: [headId] };
        const bodyRes = buildStmt(stmt.body, innerLoopCtx);
        b.addEdge(headId, bodyRes.entry || headId);
        for (const exit of bodyRes.entry ? bodyRes.exits : [headId]) b.addEdge(exit, headId);
        return { entry: headId, exits: [headId, ...innerLoopCtx.breakTargets] };
      }

      case "SwitchStatement": {
        const discId = b.addNode(stmt);
        const breakTargets = [];
        const innerLoopCtx = { breakTargets, continueTargets: loopCtx ? loopCtx.continueTargets : [] };
        let prevCaseExits = [];
        let firstCaseEntry = null; 
        const baseName = regeneratorBaseName(stmt.discriminant);
        const caseEntryByValue = new Map();
        const pendingResumes = [];
        for (const switchCase of stmt.cases) {
          const caseRes = buildStmtList(switchCase.consequent, innerLoopCtx);
          const caseEntry = caseRes.entry || b.addNode(switchCase); 
          if (firstCaseEntry === null) firstCaseEntry = caseEntry;
          b.addEdge(discId, caseEntry);
          
          for (const exit of prevCaseExits) b.addEdge(exit, caseEntry);
          prevCaseExits = caseRes.entry ? caseRes.exits : [caseEntry];

          if (switchCase.test && switchCase.test.type === "Literal") {
            caseEntryByValue.set(switchCase.test.value, caseEntry);
          }
          if (baseName) {
            const lastStmt = (switchCase.consequent || []).slice(-1)[0];
            if (lastStmt && lastStmt.type === "ReturnStatement" && caseRes.lastEntry) {
              const info = regeneratorYieldInfo(lastStmt, baseName);
              if (info) pendingResumes.push({ returnNodeId: caseRes.lastEntry, ...info });
            }
          }
        }
        for (const { returnNodeId, nextCaseValue, yieldedExpr } of pendingResumes) {
          const targetEntry = caseEntryByValue.get(nextCaseValue);
          if (!targetEntry) continue;
          b.addEdge(returnNodeId, targetEntry);
          b.regeneratorYields = b.regeneratorYields || new Map();
          b.regeneratorYields.set(returnNodeId, { baseName, yieldedExpr });
        }
        const exits = [...prevCaseExits, ...breakTargets];
        if (!stmt.cases.some(c => c.test === null)) exits.push(discId); 
        return { entry: discId, exits };
      }

      case "TryStatement": {
        const tryRes = buildStmt(stmt.block, loopCtx);
        let exits = tryRes.entry ? tryRes.exits : [];
        let entry = tryRes.entry;
        if (stmt.handler) {
          const catchRes = buildStmt(stmt.handler.body, loopCtx);
          
          if (tryRes.entry) b.addEdge(tryRes.entry, catchRes.entry || tryRes.entry);
          exits = exits.concat(catchRes.entry ? catchRes.exits : []);
          if (!entry) entry = catchRes.entry;
        }
        if (stmt.finalizer) {
          const finRes = buildStmt(stmt.finalizer, loopCtx);
          if (finRes.entry) {
            for (const exit of exits) b.addEdge(exit, finRes.entry);
            exits = finRes.exits;
            if (!entry) entry = finRes.entry;
          }
        }
        return { entry, exits };
      }

      case "BreakStatement": {
        const id = b.addNode(stmt);
        if (loopCtx) loopCtx.breakTargets.push(id);
        return { entry: id, exits: [] }; 
      }

      case "ContinueStatement": {
        const id = b.addNode(stmt);
        if (loopCtx) loopCtx.continueTargets.push(id);
        return { entry: id, exits: [] };
      }

      case "ReturnStatement": {
        const id = b.addNode(stmt);
        b.markExit(id);
        b.nodes.get(id).isReturn = true;
        return { entry: id, exits: [] };
      }

      case "ThrowStatement": {
        const id = b.addNode(stmt);
        b.markExit(id);
        b.nodes.get(id).isThrow = true;
        return { entry: id, exits: [] };
      }

      case "LabeledStatement":
        return buildStmt(stmt.body, loopCtx);

      case "EmptyStatement":
        return { entry: null, exits: [] };

      default: {
        
        const id = b.addNode(stmt);
        return { entry: id, exits: [id] };
      }
    }
  }
  const { entry, exits } = buildStmtList(statements, null);
  for (const exit of exits) b.markExit(exit);
  return { entry, nodes: b.nodes, exitNodes: b.exitNodes, regeneratorYields: b.regeneratorYields || new Map() };
}

module.exports = { buildCfg };
