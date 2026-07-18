"use strict";

function buildParentMap(root) {
  const parents = new Map();
  function walk(node, parent) {
    if (!node || typeof node !== "object") return;
    if (Array.isArray(node)) {
      for (const child of node) walk(child, parent);
      return;
    }
    if (node.type) parents.set(node, parent);
    for (const k of Object.keys(node)) {
      if (k === "type" || k === "loc" || k === "range") continue;
      walk(node[k], node);
    }
  }
  walk(root, null);
  return parents;
}

function isFunctionNode(node) {
  return node && (node.type === "FunctionExpression" || node.type === "FunctionDeclaration" || node.type === "ArrowFunctionExpression");
}

function propName(node) {
  if (!node) return null;
  if (node.type === "Identifier") return node.name;
  if (node.type === "Literal") return String(node.value);
  return null;
}

function nearestEnclosingFunction(parents, node) {
  let cur = parents.get(node);
  while (cur) {
    if (isFunctionNode(cur)) return cur;
    cur = parents.get(cur);
  }
  return null;
}

function ownerObjectLiteral(parents, fnNode) {
  const prop = parents.get(fnNode);
  if (!prop || prop.type !== "Property") return null;
  const obj = parents.get(prop);
  if (!obj || obj.type !== "ObjectExpression") return null;
  return obj;
}

function siblingMethodsOf(objectLiteral) {
  const methods = new Map();
  for (const prop of objectLiteral.properties || []) {
    const name = propName(prop.key);
    if (name && isFunctionNode(prop.value)) methods.set(name, prop.value);
  }
  return methods;
}

function classDescriptorSiblings(parents, fnNode) {
  let cur = fnNode;
  while (cur) {
    const parent = parents.get(cur);
    if (parent && parent.type === "ObjectExpression" &&
        objectPropertyValue(parent, "key") && objectPropertyValue(parent, "value")) {
      const array = parents.get(parent);
      const call = array && array.type === "ArrayExpression" ? parents.get(array) : null;
      if (call && call.type === "CallExpression") return objectMethodsFromExpression(call);
    }
    cur = parent;
  }
  return null;
}

function prototypeAssignmentBase(parents, fnNode) {
  const assign = parents.get(fnNode);
  if (!assign || assign.type !== "AssignmentExpression" || assign.right !== fnNode) return null;
  const left = assign.left;
  if (left.type !== "MemberExpression" || left.object.type !== "Identifier") return null;
  return left.object.name;
}

const prototypeSiblingsCache = new WeakMap(); 

function collectPrototypeSiblingsAllBases(root) {
  const byBase = new Map();
  function walk(n) {
    if (!n || typeof n !== "object") return;
    if (Array.isArray(n)) {
      for (const c of n) walk(c);
      return;
    }
    if (n.type === "AssignmentExpression" && n.left.type === "MemberExpression" &&
        n.left.object.type === "Identifier" && isFunctionNode(n.right)) {
      const base = n.left.object.name;
      const method = propName(n.left.property);
      if (method) {
        if (!byBase.has(base)) byBase.set(base, new Map());
        byBase.get(base).set(method, n.right);
      }
    }
    for (const k of Object.keys(n)) {
      if (k === "type" || k === "loc" || k === "range") continue;
      walk(n[k]);
    }
  }
  walk(root);
  return byBase;
}

function prototypeSiblingsOf(moduleRoot, baseName) {
  let byBase = prototypeSiblingsCache.get(moduleRoot);
  if (!byBase) {
    byBase = collectPrototypeSiblingsAllBases(moduleRoot);
    prototypeSiblingsCache.set(moduleRoot, byBase);
  }
  return byBase.get(baseName) || null;
}

function ownDeclarations(fnOrProgramNode) {
  const decls = new Map(); 
  const thisAliases = new Set(); 
  const bodyNode = fnOrProgramNode.body && fnOrProgramNode.body.type === "BlockStatement"
    ? fnOrProgramNode.body : fnOrProgramNode;
  const stmts = Array.isArray(bodyNode.body) ? bodyNode.body : [];
  for (const stmt of stmts) {
    if (stmt.type === "FunctionDeclaration" && stmt.id) {
      decls.set(stmt.id.name, stmt);
    } else if (stmt.type === "VariableDeclaration") {
      for (const decl of (Array.isArray(stmt.declarations) ? stmt.declarations : [])) {
        if (decl.id.type !== "Identifier" || !decl.init) continue;
        if (isFunctionNode(decl.init)) decls.set(decl.id.name, decl.init);
        if (decl.init.type === "ThisExpression") thisAliases.add(decl.id.name);
      }
    } else if (stmt.type === "ExpressionStatement" && stmt.expression.type === "AssignmentExpression") {
      const assign = stmt.expression;
      if (assign.left.type === "Identifier") {
        if (isFunctionNode(assign.right)) decls.set(assign.left.name, assign.right);
        if (assign.right.type === "ThisExpression") thisAliases.add(assign.left.name);
        else if (assign.right.type === "Identifier" && thisAliases.has(assign.right.name)) {
          thisAliases.add(assign.left.name); 
        }
      }
    }
  }
  return { decls, thisAliases };
}

function buildScopeChain(parents, fnNode, moduleRoot) {
  const chain = [];
  let cur = fnNode;
  while (cur) {
    const { decls, thisAliases } = ownDeclarations(cur);
    const ownerObject = isFunctionNode(cur) ? ownerObjectLiteral(parents, cur) : null;
    const base = isFunctionNode(cur) ? prototypeAssignmentBase(parents, cur) : null;
    const prototypeSiblings = base && moduleRoot ? prototypeSiblingsOf(moduleRoot, base) : null;
    const classSiblings = isFunctionNode(cur) ? classDescriptorSiblings(parents, cur) : null;
    chain.push({ decls, thisAliases, ownerObject, prototypeSiblings, classSiblings, fnNode: cur });
    cur = isFunctionNode(cur) ? nearestEnclosingFunction(parents, cur) : null;
  }
  if (moduleRoot && fnNode !== moduleRoot) {
    const { decls, thisAliases } = ownDeclarations(moduleRoot);
    chain.push({ decls, thisAliases, ownerObject: null, prototypeSiblings: null, classSiblings: null, fnNode: moduleRoot });
  }
  return chain;
}

function resolvesToThis(scopeChain, name, maxHops = 16) {
  let hops = 0;
  for (const frame of scopeChain) {
    if (frame.thisAliases.has(name)) return true;
    hops++;
    if (hops > maxHops) return false;
  }
  return false;
}

function findFunctionByName(scopeChain, name) {
  for (const frame of scopeChain) {
    if (frame.decls.has(name)) return frame.decls.get(name);
  }
  return null;
}

function findSiblingMethod(scopeChain, methodName) {
  for (const frame of scopeChain) {
    if (frame.ownerObject) {
      const siblings = siblingMethodsOf(frame.ownerObject);
      if (siblings.has(methodName)) return siblings.get(methodName);
    }
    if (frame.prototypeSiblings && frame.prototypeSiblings.has(methodName)) {
      return frame.prototypeSiblings.get(methodName);
    }
    if (frame.classSiblings && frame.classSiblings.has(methodName)) {
      return frame.classSiblings.get(methodName);
    }
  }
  return null;
}

function allSiblingMethods(scopeChain) {
  const out = new Map();
  for (const frame of scopeChain) {
    if (frame.ownerObject) {
      for (const [name, fn] of siblingMethodsOf(frame.ownerObject)) {
        if (!out.has(name)) out.set(name, fn);
      }
    }
    if (frame.prototypeSiblings) {
      for (const [name, fn] of frame.prototypeSiblings) {
        if (!out.has(name)) out.set(name, fn);
      }
    }
    if (frame.classSiblings) {
      for (const [name, fn] of frame.classSiblings) {
        if (!out.has(name)) out.set(name, fn);
      }
    }
  }
  return out;
}

function requireTargetFromExpression(expr, programModel, currentModule) {
  if (!expr || typeof expr !== "object") return null;
  if (expr.type === "CallExpression") {
    const callee = expr.callee;
    const isRequireCall = (callee.type === "Identifier" && callee.name === "require") ||
      (currentModule.requireParam && callee.type === "Identifier" && callee.name === currentModule.requireParam);
    if (isRequireCall) return programModel.resolveRequire(currentModule, expr.arguments[0]);
  }
  if (expr.type === "MemberExpression") return requireTargetFromExpression(expr.object, programModel, currentModule);
  if (expr.type === "AssignmentExpression") return requireTargetFromExpression(expr.right, programModel, currentModule);
  if (expr.type === "ConditionalExpression") {
    return requireTargetFromExpression(expr.consequent, programModel, currentModule) ||
      requireTargetFromExpression(expr.alternate, programModel, currentModule) ||
      requireTargetFromExpression(expr.test, programModel, currentModule);
  }
  if (expr.type === "LogicalExpression" || expr.type === "BinaryExpression") {
    return requireTargetFromExpression(expr.left, programModel, currentModule) ||
      requireTargetFromExpression(expr.right, programModel, currentModule);
  }
  if (expr.type === "ObjectExpression") {
    for (const prop of expr.properties || []) {
      const target = requireTargetFromExpression(prop.value, programModel, currentModule);
      if (target) return target;
    }
  }
  if (expr.type === "SequenceExpression") {
    for (const item of expr.expressions || []) {
      const target = requireTargetFromExpression(item, programModel, currentModule);
      if (target) return target;
    }
  }
  return null;
}

function findRequireBinding(scopeChain, programModel, currentModule) {
  
  const bindings = new Map();
  for (const frame of scopeChain) {
    const bodyNode = frame.fnNode.body && frame.fnNode.body.type === "BlockStatement"
      ? frame.fnNode.body : frame.fnNode;
    for (const stmt of (Array.isArray(bodyNode.body) ? bodyNode.body : [])) {
      if (stmt.type !== "VariableDeclaration") continue;
      for (const decl of (Array.isArray(stmt.declarations) ? stmt.declarations : [])) {
        if (decl.id.type !== "Identifier" || !decl.init) continue;
        const target = requireTargetFromExpression(decl.init, programModel, currentModule);
        if (target) bindings.set(decl.id.name, target);
      }
    }
  }
  return bindings;
}

function mergeMethodMaps(target, source) {
  if (!source) return target;
  for (const [name, fn] of source) target.set(name, fn);
  return target;
}

function objectPropertyValue(obj, name) {
  if (!obj || obj.type !== "ObjectExpression") return null;
  for (const prop of obj.properties || []) {
    if (!prop || prop.type !== "Property") continue;
    if (propName(prop.key) === name) return prop.value || null;
  }
  return null;
}

function functionNodeFromValue(expr) {
  if (isFunctionNode(expr)) return expr;
  let found = null;
  const seen = new WeakSet();
  function walk(node) {
    if (!node || found || typeof node !== "object" || seen.has(node)) return;
    seen.add(node);
    if (Array.isArray(node)) {
      for (const item of node) walk(item);
      return;
    }
    
    if (node.type === "CallExpression" && node.callee && node.callee.type === "MemberExpression" &&
        propName(node.callee.property) === "mark") {
      const candidate = (node.arguments || []).find(isFunctionNode);
      if (candidate) { found = candidate; return; }
    }
    for (const key of Object.keys(node)) {
      if (key === "type" || key === "loc" || key === "range") continue;
      walk(node[key]);
    }
  }
  walk(expr);
  return found;
}

function objectMethodsFromExpression(expr) {
  if (!expr) return null;
  if (expr.type === "ObjectExpression") return siblingMethodsOf(expr);
  if (expr.type === "AssignmentExpression") return objectMethodsFromExpression(expr.right);
  if (expr.type === "CallExpression") {
    const args = expr.arguments || [];
    const key = propName(args[1]);
    const value = args[2];
    const methods = new Map();
    mergeMethodMaps(methods, objectMethodsFromExpression(args[0]));
    const directFn = functionNodeFromValue(value);
    if (key && directFn) methods.set(key, directFn);
    
    for (const arg of args) {
      if (!arg || arg.type !== "ArrayExpression") continue;
      for (const descriptor of arg.elements || []) {
        if (!descriptor || descriptor.type !== "ObjectExpression") continue;
        const methodKey = objectPropertyValue(descriptor, "key");
        const methodValue = objectPropertyValue(descriptor, "value");
        const name = propName(methodKey);
        const methodFn = functionNodeFromValue(methodValue);
        if (name && methodFn) methods.set(name, methodFn);
      }
    }
    return methods.size ? methods : null;
  }
  if (expr.type === "SequenceExpression") {
    const methods = new Map();
    for (const item of expr.expressions || []) mergeMethodMaps(methods, objectMethodsFromExpression(item));
    return methods.size ? methods : null;
  }
  return null;
}

function collectAssignments(expr, out) {
  if (!expr) return;
  if (expr.type === "AssignmentExpression") {
    out.push(expr);
  } else if (expr.type === "SequenceExpression") {
    for (const item of expr.expressions || []) collectAssignments(item, out);
  }
}

function findLocalObjectMethodBinding(scopeChain) {
  const bindings = new Map();
  for (const frame of scopeChain) {
    const bodyNode = frame.fnNode.body && frame.fnNode.body.type === "BlockStatement"
      ? frame.fnNode.body : frame.fnNode;
    for (const stmt of (Array.isArray(bodyNode.body) ? bodyNode.body : [])) {
      if (stmt.type === "VariableDeclaration") {
        for (const decl of (Array.isArray(stmt.declarations) ? stmt.declarations : [])) {
          if (decl.id.type !== "Identifier" || !decl.init) continue;
          const methods = objectMethodsFromExpression(decl.init);
          if (methods && methods.size) bindings.set(decl.id.name, methods);
        }
      } else if (stmt.type === "ExpressionStatement") {
        const assignments = [];
        collectAssignments(stmt.expression, assignments);
        for (const assign of assignments) {
          if (assign.left.type !== "Identifier") continue;
          const methods = objectMethodsFromExpression(assign.right);
          if (methods && methods.size) bindings.set(assign.left.name, methods);
        }
      }
    }
  }
  return bindings;
}

function findConstructorInstanceBinding(scopeChain, requireBindings) {
  const bindings = new Map();
  function constructorTarget(expr) {
    if (!expr || expr.type !== "NewExpression") return null;
    if (expr.callee.type === "Identifier" && requireBindings.has(expr.callee.name)) {
      return { target: requireBindings.get(expr.callee.name), exportName: null };
    }
    if (expr.callee.type === "MemberExpression" && expr.callee.object.type === "Identifier" &&
        requireBindings.has(expr.callee.object.name)) {
      return { target: requireBindings.get(expr.callee.object.name), exportName: propName(expr.callee.property) };
    }
    return null;
  }
  function collectFromVarDecl(varDecl) {
    for (const decl of (Array.isArray(varDecl.declarations) ? varDecl.declarations : [])) {
      if (decl.id.type !== "Identifier" || !decl.init) continue;
      const target = constructorTarget(decl.init);
      if (target) bindings.set(decl.id.name, target);
    }
  }
  function collectFromExpression(expr) {
    const assignments = [];
    collectAssignments(expr, assignments);
    for (const assign of assignments) {
      if (assign.left.type !== "Identifier") continue;
      const target = constructorTarget(assign.right);
      if (target) bindings.set(assign.left.name, target);
    }
  }
  for (const frame of scopeChain) {
    const bodyNode = frame.fnNode.body && frame.fnNode.body.type === "BlockStatement"
      ? frame.fnNode.body : frame.fnNode;
    for (const stmt of (Array.isArray(bodyNode.body) ? bodyNode.body : [])) {
      if (stmt.type === "VariableDeclaration") {
        collectFromVarDecl(stmt);
      } else if (stmt.type === "ExpressionStatement") {
        collectFromExpression(stmt.expression);
      } else if (stmt.type === "ForStatement") {
        if (stmt.init && stmt.init.type === "VariableDeclaration") collectFromVarDecl(stmt.init);
        else collectFromExpression(stmt.init);
      } else if (stmt.type === "ForInStatement" || stmt.type === "ForOfStatement") {
        if (stmt.left && stmt.left.type === "VariableDeclaration") collectFromVarDecl(stmt.left);
        collectFromExpression(stmt.right);
      }
    }
  }
  return bindings;
}

function findGetAppBinding(scopeChain) {
  const bindings = new Set();
  function isGetAppCall(node) {
    return node && node.type === "CallExpression" &&
      node.callee.type === "Identifier" && node.callee.name === "getApp";
  }
  for (const frame of scopeChain) {
    const bodyNode = frame.fnNode.body && frame.fnNode.body.type === "BlockStatement"
      ? frame.fnNode.body : frame.fnNode;
    for (const stmt of (Array.isArray(bodyNode.body) ? bodyNode.body : [])) {
      if (stmt.type === "VariableDeclaration") {
        for (const decl of (Array.isArray(stmt.declarations) ? stmt.declarations : [])) {
          if (decl.id.type === "Identifier" && isGetAppCall(decl.init)) bindings.add(decl.id.name);
        }
      } else if (stmt.type === "ExpressionStatement" && stmt.expression.type === "AssignmentExpression") {
        const assign = stmt.expression;
        if (assign.left.type === "Identifier" && isGetAppCall(assign.right)) bindings.add(assign.left.name);
      }
    }
  }
  return bindings;
}

const appObjectMethodsCache = new WeakMap();

function appObjectMethods(appModule) {
  if (!appModule || !appModule.bodyNode) return null;
  let methods = appObjectMethodsCache.get(appModule.bodyNode);
  if (methods) return methods;
  methods = new Map();
  function walk(n) {
    if (!n || typeof n !== "object") return;
    if (Array.isArray(n)) {
      for (const c of n) walk(c);
      return;
    }
    if (n.type === "CallExpression" && n.callee.type === "Identifier" && n.callee.name === "App" &&
        n.arguments && n.arguments[0] && n.arguments[0].type === "ObjectExpression") {
      for (const [name, fn] of siblingMethodsOf(n.arguments[0])) methods.set(name, fn);
      return;
    }
    for (const k of Object.keys(n)) {
      if (k === "type" || k === "loc" || k === "range") continue;
      walk(n[k]);
    }
  }
  walk(appModule.bodyNode);
  appObjectMethodsCache.set(appModule.bodyNode, methods);
  return methods;
}

function topLevelObjectMethodBindings(moduleNode) {
  const bindings = new Map();
  const stmts = moduleNode.body || [];
  for (const stmt of stmts) {
    if (stmt.type === "VariableDeclaration") {
      for (const decl of (Array.isArray(stmt.declarations) ? stmt.declarations : [])) {
        if (decl.id.type !== "Identifier" || !decl.init) continue;
        const methods = objectMethodsFromExpression(decl.init);
        if (methods && methods.size) bindings.set(decl.id.name, methods);
      }
    } else if (stmt.type === "ExpressionStatement") {
      const assignments = [];
      collectAssignments(stmt.expression, assignments);
      for (const assign of assignments) {
        if (assign.left.type !== "Identifier") continue;
        const methods = objectMethodsFromExpression(assign.right);
        if (methods && methods.size) bindings.set(assign.left.name, methods);
      }
    }
  }
  return bindings;
}

function topLevelRequireBindings(module, programModel) {
  const bindings = new Map();
  if (!module || !module.bodyNode || !programModel) return bindings;
  for (const stmt of module.bodyNode.body || []) {
    if (stmt.type !== "VariableDeclaration") continue;
    for (const decl of stmt.declarations || []) {
      if (!decl || decl.id.type !== "Identifier" || !decl.init) continue;
      const target = requireTargetFromExpression(decl.init, programModel, module);
      if (target) bindings.set(decl.id.name, target);
    }
  }
  return bindings;
}

function returnedExpression(fnNode) {
  if (!isFunctionNode(fnNode)) return null;
  if (fnNode.body && fnNode.body.type !== "BlockStatement") return fnNode.body;
  for (const stmt of (fnNode.body && fnNode.body.body) || []) {
    if (stmt.type === "ReturnStatement" && stmt.argument) return stmt.argument;
  }
  return null;
}

function exportEntryFromExpression(expr, objectBindings, requireBindings, programModel, visiting) {
  if (!expr) return null;
  if (expr.type === "Identifier") {
    if (objectBindings.has(expr.name)) return { objectMethods: objectBindings.get(expr.name) };
    return { ref: expr.name };
  }
  if (expr.type === "ObjectExpression") {
    const methods = objectMethodsFromExpression(expr);
    return methods && methods.size ? { objectMethods: methods } : null;
  }
  if (expr.type === "MemberExpression" && expr.object.type === "Identifier") {
    const imported = requireBindings.get(expr.object.name);
    const property = propName(expr.property);
    if (imported && property) {
      const nested = moduleExportsObject(imported, programModel, visiting);
      const entry = nested.get(property);
      return entry ? { ...entry, module: entry.module || imported } : null;
    }
  }
  return null;
}

function moduleExportsObject(targetModule, programModel, visiting = new Set()) {
  
  const exportsMap = new Map();
  if (!targetModule || !targetModule.bodyNode || visiting.has(targetModule)) return exportsMap;
  visiting.add(targetModule);
  const bodyNode = targetModule.bodyNode;
  const objectBindings = topLevelObjectMethodBindings(bodyNode);
  const requireBindings = topLevelRequireBindings(targetModule, programModel);
  const stmts = bodyNode.body || [];
  function collectAssignments(expr, out) {
    if (!expr) return;
    if (expr.type === "AssignmentExpression") {
      out.push(expr);
      return;
    }
    if (expr.type === "SequenceExpression") {
      for (const item of expr.expressions || []) collectAssignments(item, out);
    }
  }
  function collectCalls(node, out) {
    if (!node || typeof node !== "object") return;
    if (Array.isArray(node)) {
      for (const item of node) collectCalls(item, out);
      return;
    }
    if (node.type === "CallExpression") out.push(node);
    if (node.type === "FunctionExpression" || node.type === "FunctionDeclaration" || node.type === "ArrowFunctionExpression") return;
    for (const key of Object.keys(node)) {
      if (key === "type" || key === "loc" || key === "range") continue;
      collectCalls(node[key], out);
    }
  }
  for (const stmt of stmts) {
    if (stmt.type !== "ExpressionStatement") continue;
    const calls = [];
    collectCalls(stmt.expression, calls);
    for (const call of calls) {
      if (!call.callee || call.callee.type !== "MemberExpression" || propName(call.callee.property) !== "d") continue;
      const args = call.arguments || [];
      if (!args[0] || args[0].type !== "Identifier" || args[0].name !== targetModule.exportsParam) continue;
      const descriptors = args[1];
      if (!descriptors || descriptors.type !== "ObjectExpression") continue;
      for (const prop of descriptors.properties || []) {
        if (!prop || prop.type !== "Property") continue;
        const name = propName(prop.key);
        const value = returnedExpression(prop.value);
        const entry = name && exportEntryFromExpression(value, objectBindings, requireBindings, programModel, visiting);
        if (name && entry) exportsMap.set(name, entry);
      }
    }
  }
  for (const stmt of stmts) {
    if (stmt.type !== "ExpressionStatement") continue;
    const assignments = [];
    collectAssignments(stmt.expression, assignments);
    for (const assign of assignments) {
      const left = assign.left;
      const right = assign.right;
      if (left.type !== "MemberExpression") continue;
      const moduleName = targetModule.moduleParam || "module";
      const exportsName = targetModule.exportsParam || "exports";
      const objName = left.object.type === "Identifier" ? left.object.name : null;
      const isModuleExports = objName === moduleName && propName(left.property) === "exports";
      const isExportsDotX = objName === exportsName;
      const isModuleExportsDotX = left.object.type === "MemberExpression" &&
        left.object.object.type === "Identifier" && left.object.object.name === moduleName &&
        propName(left.object.property) === "exports";
      if (isModuleExports) {
        if (isFunctionNode(right)) {
          exportsMap.set("default", right);
        } else if (right.type === "ObjectExpression") {
          for (const prop of right.properties || []) {
            if (!prop || prop.type !== "Property") continue;
            const name = propName(prop.key);
            if (!name) continue;
            if (isFunctionNode(prop.value)) {
              exportsMap.set(name, prop.value);
            } else if (prop.value.type === "Identifier" && objectBindings.has(prop.value.name)) {
              const methods = objectBindings.get(prop.value.name);
              exportsMap.set(name, { objectMethods: methods });
              for (const [methodName, fn] of methods) if (!exportsMap.has(methodName)) exportsMap.set(methodName, fn);
            } else {
              const methods = objectMethodsFromExpression(prop.value);
              if (methods && methods.size) {
                exportsMap.set(name, { objectMethods: methods });
                for (const [methodName, fn] of methods) if (!exportsMap.has(methodName)) exportsMap.set(methodName, fn);
              }
            }
          }
        } else if (right.type === "Identifier") {
          if (objectBindings.has(right.name)) exportsMap.set("default", { objectMethods: objectBindings.get(right.name) });
          else exportsMap.set("default", { ref: right.name });
        }
      } else if (isExportsDotX || isModuleExportsDotX) {
        const name = propName(left.property);
        if (!name) continue;
        if (isFunctionNode(right)) exportsMap.set(name, right);
        else if (right.type === "Identifier") {
          if (objectBindings.has(right.name)) exportsMap.set(name, { objectMethods: objectBindings.get(right.name) });
          else exportsMap.set(name, { ref: right.name });
        }
      }
    }
  }
  return exportsMap;
}

function findInitializerInFunction(fnNode, name) {
  const body = fnNode && fnNode.body && fnNode.body.type === "BlockStatement" ? fnNode.body.body : [];
  for (const stmt of body || []) {
    if (stmt.type !== "VariableDeclaration") continue;
    for (const decl of stmt.declarations || []) {
      if (decl.id && decl.id.type === "Identifier" && decl.id.name === name && decl.init) return decl.init;
    }
  }
  return null;
}

function constructorDescriptor(expr, fnNode, programModel, currentModule) {
  if (!expr || expr.type !== "NewExpression") return null;
  let callee = expr.callee;
  if (callee.type === "Identifier") {
    const init = findInitializerInFunction(fnNode, callee.name);
    if (init) callee = init;
  }
  const target = requireTargetFromExpression(callee, programModel, currentModule);
  if (!target) return null;
  const exportName = callee.type === "MemberExpression" ? propName(callee.property) : null;
  return { target, exportName };
}

function findThisFieldConstructorBinding(memberExpr, scopeChain, currentModule, programModel) {
  if (!memberExpr || memberExpr.type !== "MemberExpression" || memberExpr.object.type !== "ThisExpression") return null;
  const field = propName(memberExpr.property);
  if (!field) return null;
  for (const fnNode of allSiblingMethods(scopeChain).values()) {
    let found = null;
    function walk(node) {
      if (!node || found || typeof node !== "object") return;
      if (Array.isArray(node)) { for (const item of node) walk(item); return; }
      if (node !== fnNode && isFunctionNode(node)) return;
      if (node.type === "AssignmentExpression" && node.left.type === "MemberExpression" &&
          node.left.object.type === "ThisExpression" && propName(node.left.property) === field) {
        found = constructorDescriptor(node.right, fnNode, programModel, currentModule);
        return;
      }
      for (const key of Object.keys(node)) {
        if (key === "type" || key === "loc" || key === "range") continue;
        walk(node[key]);
      }
    }
    walk(fnNode.body);
    if (found) return found;
  }
  return null;
}

function resolveRequireMemberPath(memberExpr, scopeChain, currentModule, programModel) {
  const path = [];
  let cur = memberExpr;
  while (cur && cur.type === "MemberExpression") {
    const part = propName(cur.property);
    if (!part) return null;
    path.unshift(part);
    cur = cur.object;
  }
  if (!cur || cur.type !== "Identifier" || !path.length) return null;
  const bindings = findRequireBinding(scopeChain, programModel, currentModule);
  const target = bindings.get(cur.name);
  if (!target) return null;
  let entry = moduleExportsObject(target, programModel).get(path.shift());
  let entryModule = target;
  while (entry && path.length) {
    const next = path.shift();
    if (entry.objectMethods) {
      const fn = entry.objectMethods.get(next);
      entry = fn || null;
      continue;
    }
    return null;
  }
  return entry ? { target: entry.module || entryModule, entry } : null;
}

function resolveCallee(callExpr, parents, currentModule, programModel) {
  const callee = callExpr.callee;
  const enclosingFn = nearestEnclosingFunction(parents, callExpr) || currentModule.bodyNode;
  const scopeChain = buildScopeChain(parents, enclosingFn, currentModule.bodyNode);

  if (callee.type === "Identifier") {
    const local = findFunctionByName(scopeChain, callee.name);
    return local ? { fnNode: local, module: currentModule } : null;
  }

  if (callee.type === "MemberExpression") {
    const methodName = propName(callee.property);
    if (!methodName) return null;

    if (callee.object.type === "ThisExpression") {
      const fn = findSiblingMethod(scopeChain, methodName);
      return fn ? { fnNode: fn, module: currentModule } : null;
    }
    if (callee.object.type === "MemberExpression") {
      const instance = findThisFieldConstructorBinding(callee.object, scopeChain, currentModule, programModel);
      if (instance) {
        const exportsMap = moduleExportsObject(instance.target, programModel);
        const entry = instance.exportName ? exportsMap.get(instance.exportName) : exportsMap.get("default");
        const resolved = resolveExportEntry(instance.target, entry, methodName);
        if (resolved) return resolved;
      }
    }
    if (callee.object.type === "Identifier") {
      if (resolvesToThis(scopeChain, callee.object.name)) {
        const fn = findSiblingMethod(scopeChain, methodName);
        return fn ? { fnNode: fn, module: currentModule } : null;
      }
      const getAppBindings = findGetAppBinding(scopeChain);
      if (getAppBindings.has(callee.object.name)) {
        const methods = appObjectMethods(programModel && programModel.appModule);
        const fn = methods && methods.get(methodName);
        if (fn) return { fnNode: fn, module: programModel.appModule };
      }
      const requireBindings = findRequireBinding(scopeChain, programModel, currentModule);
      const constructorInstances = findConstructorInstanceBinding(scopeChain, requireBindings);
      if (constructorInstances.has(callee.object.name)) {
        const descriptor = constructorInstances.get(callee.object.name);
        const target = descriptor.target || descriptor;
        const exportsMap = moduleExportsObject(target, programModel);
        const preferred = descriptor.exportName ? exportsMap.get(descriptor.exportName) : exportsMap.get("default");
        return resolveExportEntry(target, preferred, methodName) ||
          resolveExportEntry(target, exportsMap.get(methodName), methodName);
      }
      if (requireBindings.has(callee.object.name)) {
        const target = requireBindings.get(callee.object.name);
        const exportsMap = moduleExportsObject(target, programModel);
        return resolveExportEntry(target, exportsMap.get(methodName), methodName);
      }
      const localObjectMethods = findLocalObjectMethodBinding(scopeChain);
      if (localObjectMethods.has(callee.object.name)) {
        const fn = localObjectMethods.get(callee.object.name).get(methodName);
        if (fn) return { fnNode: fn, module: currentModule };
      }
    }
    if (callee.object.type === "CallExpression" &&
        callee.object.callee.type === "Identifier" &&
        callee.object.callee.name === "getApp") {
      const methods = appObjectMethods(programModel && programModel.appModule);
      const fn = methods && methods.get(methodName);
      if (fn) return { fnNode: fn, module: programModel.appModule };
    }
    if (callee.object.type === "MemberExpression") {
      const resolvedPath = resolveRequireMemberPath(callee.object, scopeChain, currentModule, programModel);
      if (resolvedPath) return resolveExportEntry(resolvedPath.target, resolvedPath.entry, methodName);
    }
    
    if (callee.object.type === "MemberExpression" && callee.object.object.type === "Identifier" &&
        propName(callee.object.property) === "default") {
      const requireBindings = findRequireBinding(scopeChain, programModel, currentModule);
      const baseName = callee.object.object.name;
      if (requireBindings.has(baseName)) {
        const target = requireBindings.get(baseName);
        const exportsMap = moduleExportsObject(target, programModel);
        return resolveExportEntry(target, exportsMap.get("default"), methodName) ||
          resolveExportEntry(target, exportsMap.get(methodName), methodName);
      }
    }
  }
  return null;
}

function resolveExportEntry(target, entry, methodName) {
  if (!entry) return null;
  if (entry.objectMethods) {
    const fn = entry.objectMethods.get(methodName);
    return fn ? { fnNode: fn, module: target } : null;
  }
  if (entry.ref) {
    const siblings = prototypeSiblingsOf(target.bodyNode, entry.ref);
    const fn = siblings && siblings.get(methodName);
    return fn ? { fnNode: fn, module: target } : null;
  }
  return { fnNode: entry, module: target };
}

module.exports = {
  buildParentMap, isFunctionNode, propName, nearestEnclosingFunction,
  buildScopeChain, resolvesToThis, findFunctionByName, findSiblingMethod,
  findRequireBinding, requireTargetFromExpression, findLocalObjectMethodBinding, findConstructorInstanceBinding, findGetAppBinding, appObjectMethods, moduleExportsObject, allSiblingMethods, resolveCallee,
};
