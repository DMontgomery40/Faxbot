/* Static request discovery for test_console_cli_parity.py. Uses the TypeScript
 * compiler's resolved signatures, not string matches or names shared by unrelated
 * objects. Only calls in screens (and helpers reached from them) are roots. */
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(process.argv[2]);
const ts = require(path.join(__dirname, '..', 'admin_ui', 'node_modules', 'typescript'));
const HOLE = '\x00';
function files(dir) {
  return fs.readdirSync(dir, {withFileTypes: true}).flatMap(entry => {
    if (['__tests__', 'test', 'mocks'].includes(entry.name)) return [];
    const file = path.join(dir, entry.name);
    return entry.isDirectory() ? files(file) : /\.(ts|tsx)$/.test(file) && !/\.(test|spec)\.tsx?$/.test(file) ? [file] : [];
  });
}
const sourceFiles = files(root);
const program = ts.createProgram(sourceFiles, {target: ts.ScriptTarget.ESNext,
  module: ts.ModuleKind.ESNext, moduleResolution: ts.ModuleResolutionKind.Bundler,
  jsx: ts.JsxEmit.ReactJSX, skipLibCheck: true, noEmit: true});
const checker = program.getTypeChecker();
const clientFile = path.join(root, 'api', 'client.ts');
const requests = new Map();
const methodRequests = {};
const unique = values => [...new Set(values)];
function unwrap(node) {
  while (node && (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) || ts.isNonNullExpression(node))) node = node.expression;
  return node;
}
function symbol(node) {
  let result = checker.getSymbolAtLocation(node);
  if (result && (result.flags & ts.SymbolFlags.Alias)) result = checker.getAliasedSymbol(result);
  return result;
}
function values(node, env, seen = new Set()) {
  node = unwrap(node);
  if (!node) return [];
  if (ts.isStringLiteralLike(node)) return [node.text];
  if (ts.isTemplateExpression(node)) {
    let result = [node.head.text];
    for (const span of node.templateSpans) result = result.flatMap(prefix =>
      (values(span.expression, env, seen).filter(v => typeof v === 'string').length ? values(span.expression, env, seen) : [HOLE])
        .map(value => prefix + value + span.literal.text));
    return unique(result);
  }
  if (ts.isConditionalExpression(node)) return unique([...values(node.whenTrue, env, seen), ...values(node.whenFalse, env, seen)]);
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
    const left = values(node.left, env, seen), right = values(node.right, env, seen);
    return (left.length ? left : [HOLE]).flatMap(a => (right.length ? right : [HOLE]).map(b => a + b));
  }
  if (ts.isObjectLiteralExpression(node)) {
    let result = {};
    for (const prop of node.properties) {
      if (ts.isPropertyAssignment(prop)) result[prop.name.getText().replace(/^['"]|['"]$/g, '')] = values(prop.initializer, env, seen);
      if (ts.isShorthandPropertyAssignment(prop)) {
        const target = checker.getShorthandAssignmentValueSymbol(prop);
        result[prop.name.text] = target && env.has(target) ? env.get(target) : values(prop.name, env, seen);
        if (!result[prop.name.text].length && target) result[prop.name.text] = (target.declarations || []).flatMap(decl => decl.initializer ? values(decl.initializer, env, seen) : []);
      }
    }
    return [result];
  }
  if (ts.isPropertyAccessExpression(node)) {
    const objects = values(node.expression, env, seen);
    const props = objects.flatMap(object => typeof object === 'object' ? object[node.name.text] || [] : []);
    if (props.length) return props;
  }
  if (ts.isCallExpression(node)) {
    const decl = implementation(node);
    if (decl && decl.body && !seen.has(decl)) {
      const bindings = new Map(env);
      (decl.parameters || []).forEach((param, i) => {
        const sym = symbol(param.name);
        if (sym) bindings.set(sym, node.arguments[i] ? values(node.arguments[i], env, seen) : values(param.initializer, env, seen));
      });
      const body = unwrap(decl.body);
      if (!ts.isBlock(body)) return values(body, bindings, new Set(seen).add(decl));
      return body.statements.filter(ts.isReturnStatement).flatMap(stmt => values(stmt.expression, bindings, new Set(seen).add(decl)));
    }
  }
  const sym = symbol(ts.isPropertyAccessExpression(node) ? node.name : node);
  if (sym && env.has(sym)) return env.get(sym);
  if (sym && !seen.has(sym)) {
    const next = new Set(seen).add(sym);
    const result = (sym.declarations || []).flatMap(decl => decl.initializer ? values(decl.initializer, env, next) : []);
    if (result.length) return result;
  }
  return [];
}
function record(methods, paths, node, found) {
  for (const method of methods) for (const value of paths) {
    if (typeof method !== 'string' || !/^(GET|POST|PUT|PATCH|DELETE|OPTIONS|WS)$/.test(method) || typeof value !== 'string') continue;
    if (!value.includes('/')) continue;
    const key = method + ' ' + value;
    const file = node.getSourceFile();
    found.set(key, {method, path: value, source: path.relative(root, file.fileName) + ':' + (file.getLineAndCharacterOfPosition(node.pos).line + 1)});
  }
}
function implementation(node) {
  const signature = ts.isCallExpression(node) ? checker.getResolvedSignature(node) : null;
  let decl = signature && signature.declaration;
  if (decl && !decl.body && (ts.isMethodSignature(decl) || ts.isFunctionTypeNode(decl))) {
    const expr = unwrap(node.expression);
    const name = ts.isPropertyAccessExpression(expr) ? expr.name.text : '';
    const source = decl.getSourceFile();
    // A helper can advertise an explicit interface rather than its inferred
    // return type. Find its implementation in that same source module only.
    if (name && source.fileName.startsWith(root + path.sep)) {
      const matches = [];
      function visit(child) {
        if (ts.isPropertyAssignment(child) && child.name.getText() === name && ts.isArrowFunction(child.initializer)) matches.push(child.initializer);
        ts.forEachChild(child, visit);
      }
      visit(source);
      const returns = checker.typeToString(checker.getReturnTypeOfSignature(signature));
      const compatible = matches.filter(candidate => {
        const signatures = checker.getTypeAtLocation(candidate).getCallSignatures();
        return signatures.some(sig => checker.typeToString(checker.getReturnTypeOfSignature(sig)) === returns);
      });
      if (compatible.length === 1) decl = compatible[0];
      else if (matches.length === 1) decl = matches[0];
    }
  }
  return decl;
}
function invoke(node, env, stack, found) {
  const expr = unwrap(node.expression);
  const name = ts.isPropertyAccessExpression(expr) ? expr.name.text : ts.isIdentifier(expr) ? expr.text : '';
  const args = [...(node.arguments || [])];
  const decl = implementation(node);
  const admin = decl && path.resolve(decl.getSourceFile().fileName) === clientFile;
  // These are the HTTP leaves; unlike an arbitrary method called "get", their
  // declarations establish which client is doing the request.
  if ((admin && ['json', 'fetch', 'send'].includes(name)) || (name === 'fetch' && ts.isIdentifier(expr))) {
    const options = values(args[1], env);
    const methods = options.length ? options.flatMap(option => option.method || ['GET']) : ['GET'];
    record(methods, values(args[0], env), node, found); return;
  }
  if (admin && name === 'accessWrite') {record(values(args[2], env).length ? values(args[2], env) : ['POST'], values(args[0], env), node, found); return;}
  if ((admin && name === 'call') || ['call', 'send'].includes(name) && (!decl || !decl.body) && values(args[0], env).some(value => value && value.method && value.path)) {
    for (const request of values(args[0], env)) if (request && typeof request === 'object') record(request.method || [], request.path || [], node, found);
    return;
  }
  if (name === 'WebSocket') {record(['WS'], values(args[0], env), node, found); return;}
  if (!decl || !decl.body || !path.resolve(decl.getSourceFile().fileName).startsWith(root + path.sep) || stack.has(decl)) return;
  const bindings = new Map(env);
  (decl.parameters || []).forEach((param, i) => {
    const sym = symbol(param.name);
    if (sym) bindings.set(sym, args[i] ? values(args[i], env) : values(param.initializer, env));
  });
  const next = new Set(stack).add(decl);
  function visit(child) {
    // A closure is analyzed when invoked. Traversing declarations here would
    // incorrectly count unused methods in a returned API helper object.
    if (child !== decl.body && ts.isFunctionLike(child)) return;
    if (ts.isCallExpression(child) || ts.isNewExpression(child)) invoke(child, bindings, next, found);
    ts.forEachChild(child, visit);
  }
  visit(decl.body);
}
for (const fileName of sourceFiles) {
  const file = program.getSourceFile(fileName);
  if (fileName.startsWith(path.join(root, 'api') + path.sep)) continue;
  // Plain .ts helper factories are reached by signature resolution from their
  // callers. TSX components, hooks and the application shell supply roots.
  if (!fileName.endsWith('.tsx') && !fileName.includes(path.sep + 'hooks' + path.sep)) continue;
  function visit(node) {
    if (ts.isCallExpression(node) || ts.isNewExpression(node)) invoke(node, new Map(), new Set(), requests);
    ts.forEachChild(node, visit);
  }
  visit(file);
}
const client = program.getSourceFile(clientFile);
function members(node) {
  if (ts.isMethodDeclaration(node) && node.body) {
    const found = new Map();
    function visit(child) {
      if (ts.isCallExpression(child) || ts.isNewExpression(child)) invoke(child, new Map(), new Set([node]), found);
      ts.forEachChild(child, visit);
    }
    visit(node.body);
    methodRequests[node.name.getText()] = [...found.values()];
  }
  ts.forEachChild(node, members);
}
members(client);
process.stdout.write(JSON.stringify({calls: [...requests.values()], methods: methodRequests}));
