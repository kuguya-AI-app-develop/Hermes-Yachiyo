import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import ts from 'typescript';

const source = readFileSync(new URL('../src/features/yachiyo-chat/markdown.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } });
const { renderMarkdown } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);

function assertDiff(text, expectedLineClass) {
  const html = renderMarkdown(text, 'diff-proof');
  assert.match(html, /markdown-code-block-diff/);
  assert.match(html, new RegExp(expectedLineClass));
  assert.doesNotMatch(html, /<(?:ul|ol|li)>/);
  assert.match(html, /\bdata-code-copy(?:\s|=)/);
  return html;
}

test('a bare diff with only added lines renders as code', () => {
  assertDiff('--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+ new file\n+ new line', 'diff-line-add');
});
test('a resumed transcript with only deleted lines renders as code', () => {
  assertDiff('Resumed session example\nreview diff\n--- a/old.txt\n+++ /dev/null\n@@ -1,2 +0,0 @@\n- old file\n- old line', 'diff-line-delete');
});
test('a diff adding an empty line renders as code', () => {
  assertDiff('--- a/file\n+++ b/file\n@@ -0,0 +1 @@\n+', 'diff-line-add');
});
test('changed content beginning with another sign remains a diff', () => {
  assertDiff('--- a/file\n+++ b/file\n@@ -1 +1 @@\n--old\n++new', 'diff-line-add');
});
test('changed lines resembling file headers remain code inside a hunk', () => {
  assertDiff('--- a/file\n+++ b/file\n@@ -0,0 +1 @@\n+++ content', 'diff-line-hunk');
  assertDiff('--- a/file\n+++ b/file\n@@ -1 +0,0 @@\n--- content', 'diff-line-hunk');
});
test('mixed diffs keep highlighting and escape embedded markup', () => {
  const html = assertDiff('Resumed session example\nreview diff\n--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+<script>new</script>', 'diff-line-delete');
  assert.match(html, /&lt;script&gt;new&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});
test('ordinary plus and minus Markdown lists stay lists', () => {
  const html = renderMarkdown('+ first\n+ second\n\n- third\n- fourth');
  assert.doesNotMatch(html, /markdown-code-block-diff/);
  assert.equal((html.match(/<li>/g) || []).length, 4);
});
test('file headers without changed lines are not classified as a diff', () => {
  const html = renderMarkdown('--- a/file\n+++ b/file\n@@ -1 +1 @@\n unchanged');
  assert.doesNotMatch(html, /markdown-code-block-diff/);
});
test('explicit fenced diff blocks remain supported', () => {
  assertDiff('```diff\n--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+new\n```', 'diff-line-add');
});
