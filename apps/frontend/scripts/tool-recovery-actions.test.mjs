import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import test from 'node:test';
import ts from 'typescript';

const sourceUrl = new URL('../src/features/runtime-shared/toolRecoveryActions.ts', import.meta.url);
const output = ts.transpileModule(readFileSync(sourceUrl, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const loaded = { exports: {} };
vm.runInNewContext(output, {
  exports: loaded.exports, module: loaded, require: createRequire(sourceUrl),
}, { filename: fileURLToPath(sourceUrl) });
const {
  runtimeToolRecoveryActionPrompt,
  runtimeToolRecoveryActionTaskStart,
  runtimeToolRecoveryActionWithInputPatch,
} = loaded.exports;

function action(tool, input, prompt = '打开链接') {
  return { tool, input, prompt, label: prompt, permission_target: 'desktop_control' };
}

test('a URL edit replaces an incomplete or stale executable prompt', () => {
  const original = action('browser.open_url', { url: 'https://github.com' });
  const edited = runtimeToolRecoveryActionWithInputPatch(original, { url: 'https://example.com' });
  assert.equal(runtimeToolRecoveryActionPrompt(original), '打开 https://github.com');
  assert.equal(runtimeToolRecoveryActionPrompt(edited), '打开 https://example.com');
  const start = runtimeToolRecoveryActionTaskStart(edited);
  assert.equal(start.prompt, '打开 https://example.com');
  assert.equal(start.metadata.recovery_input.url, 'https://example.com');
  assert.equal(original.input.url, 'https://github.com');
});

test('selected app and volume replace stale saved labels', () => {
  assert.equal(runtimeToolRecoveryActionPrompt(action('app.open', { app_name: 'Finder' }, '打开Safari')), '打开Finder');
  assert.equal(runtimeToolRecoveryActionPrompt(action('system.volume', { action: 'set', level: 35 }, '设置音量')), '把音量调到 35%');
});

test('typing recovery carries exact target and text after editing', () => {
  const original = action('app.open_and_type_into_ui_element', {
    app_name: 'WeChat', target: '消息', text: '旧文本', role_filter: 'text', limit: 80,
  }, '打开WeChat并输入文字');
  const text = ' new text, "quoted"\n然后打开 Safari ';
  const edited = runtimeToolRecoveryActionWithInputPatch(original, { text });
  const start = runtimeToolRecoveryActionTaskStart(edited);
  assert.equal(start.prompt, `打开WeChat并在前台控件"消息"输入${JSON.stringify(text)}`);
  assert.equal(start.metadata.recovery_input.text, text);
  assert.equal(original.input.text, '旧文本');
});

test('a control observation retains its selected role', () => {
  assert.equal(runtimeToolRecoveryActionPrompt(action('desktop.ui_elements', { role_filter: 'button', limit: 80 }, '查看控件')), '查看当前界面按钮');
});

test('native recovery prompts identify the selected observation and application', () => {
  const cases = [
    ['media.apple_music_control', { action: 'pause' }, '暂停 Apple Music'],
    ['media.apple_music_play', { query: '超时空辉夜姬' }, '在 Apple Music 中播放 超时空辉夜姬'],
    ['media.music_app_open_and_play', { app_name: 'Music' }, '打开Apple Music并播放'],
    ['media.music_app_open_and_play', { app_name: 'Spotify' }, '打开Spotify并播放'],
    ['clipboard.write', { text: 'hello' }, '把 hello 复制到剪贴板'],
    ['browser.current_page', {}, '读取当前网页标题和地址'],
    ['desktop.running_apps', {}, '列出当前运行的应用'],
  ];
  for (const [tool, input, expected] of cases) {
    assert.equal(runtimeToolRecoveryActionPrompt(action(tool, input, '恢复操作')), expected);
  }
});

test('the native Music alias retains its selected application in task metadata', () => {
  const start = runtimeToolRecoveryActionTaskStart(action(
    'media.music_app_open_and_play', { app_name: 'Music' }, '打开Music并播放',
  ));
  assert.equal(start.prompt, '打开Apple Music并播放');
  assert.equal(start.metadata.recovery_tool, 'media.music_app_open_and_play');
  assert.equal(start.metadata.recovery_input.app_name, 'Music');
});

test('unsupported recovery tools preserve the explicit custom prompt', () => {
  assert.equal(runtimeToolRecoveryActionPrompt(action('custom.tool', {}, '打开专用工作流')), '打开专用工作流');
});
