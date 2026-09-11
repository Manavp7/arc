/** Run source-level regression tests with the project's own TypeScript compiler. */
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
export async function resolve(specifier, context, nextResolve) {
  try { return await nextResolve(specifier, context); }
  catch (error) {
    if (error.code !== 'ERR_MODULE_NOT_FOUND' || !specifier.startsWith('.')) throw error;
    for (const extension of ['.ts', '.tsx']) {
      try { return await nextResolve(`${specifier}${extension}`, context); } catch { /* Try the next source extension. */ }
    }
    throw error;
  }
}
export async function load(url, context, nextLoad) {
  if (!/\.tsx?$/.test(new URL(url).pathname) || url.includes('/node_modules/')) return nextLoad(url, context);
  const source = await readFile(new URL(url), 'utf8');
  return { format: 'module', shortCircuit: true, source: ts.transpileModule(source, {
    fileName: new URL(url).pathname,
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX },
  }).outputText };
}
