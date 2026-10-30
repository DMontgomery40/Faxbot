/* Source reachability, not a rendered usability claim. Follow value imports used
 * by the application, excluding dead imports, type imports and orphan screens. */
const path = require('node:path');
const ts = require('../admin_ui/node_modules/typescript');

function reachableFiles(root, entry = 'main.tsx') {
  root = path.resolve(root);
  const program = ts.createProgram([path.join(root, entry)], {
    target: ts.ScriptTarget.ESNext, module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler, jsx: ts.JsxEmit.ReactJSX,
    allowImportingTsExtensions: true, noEmit: true, skipLibCheck: true,
  });
  const checker = program.getTypeChecker();
  const reached = new Set();
  function follow(file) {
    if (!file || reached.has(file.fileName) || !file.fileName.startsWith(root + path.sep)) return;
    if (/[/\\](__tests__|test|mocks)[/\\]|\.(test|spec)\.tsx?$/.test(file.fileName)) return;
    reached.add(file.fileName);
    function visit(node) {
      if (ts.isImportDeclaration(node) || ts.isTypeNode(node)) return;
      if (ts.isIdentifier(node)) {
        const symbol = checker.getSymbolAtLocation(node);
        if (symbol && symbol.flags & ts.SymbolFlags.Alias) {
          for (const declaration of checker.getAliasedSymbol(symbol).declarations || []) {
            follow(declaration.getSourceFile());
          }
        }
      }
      ts.forEachChild(node, visit);
    }
    visit(file);
  }
  follow(program.getSourceFile(path.join(root, entry)));
  return [...reached].sort();
}
module.exports = { reachableFiles };
if (require.main === module) process.stdout.write(JSON.stringify(reachableFiles(process.argv[2], process.argv[3])));
