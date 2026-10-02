const CODE_COPY_ICON_HTML = '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="8" width="11" height="11" rx="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v1"></path></svg>';
const CODE_CHECK_ICON_HTML = '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12.5 4.2 4.2L19 7"></path></svg>';

export function fencedCode(code: string, language: string) {
  const safeCode = String(code || '').replace(/```/g, '`\\`\\`');
  return `\`\`\`${language || 'text'}\n${safeCode}\n\`\`\``;
}

export function renderMarkdown(text: string, messageId = '', copiedCodeBlockKey = '') {
  const source = String(text || '').replace(/\r\n/g, '\n');
  if (!source) return '';
  const standaloneCode = detectStandaloneCodeBlock(source);
  if (standaloneCode) {
    return renderCodeBlockHtml(standaloneCode.code, standaloneCode.language, messageId, copiedCodeBlockKey, 0);
  }

  const lines = source.split('\n');
  let html = '';
  let paragraph: string[] = [];
  let listType: 'ul' | 'ol' | null = null;
  let inCode = false;
  let codeFenceMarker = '';
  let codeLines: string[] = [];
  let codeLanguage = '';
  let codeBlockIndex = 0;

  function flushParagraph() {
    if (paragraph.length === 0) return;
    html += `<p>${paragraph.map(renderInlineMarkdown).join('<br>')}</p>`;
    paragraph = [];
  }

  function closeList() {
    if (!listType) return;
    html += `</${listType}>`;
    listType = null;
  }

  function openList(type: 'ul' | 'ol') {
    if (listType === type) return;
    closeList();
    listType = type;
    html += `<${type}>`;
  }

  function flushCode() {
    const code = codeLines.join('\n');
    if (isInternalTaskJsonText(code)) {
      html += renderInternalTaskJsonBlock(code);
    } else {
      html += renderCodeBlockHtml(code, codeLanguage, messageId, copiedCodeBlockKey, codeBlockIndex);
    }
    codeLines = [];
    codeLanguage = '';
    codeFenceMarker = '';
    inCode = false;
    codeBlockIndex += 1;
  }

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const fence = parseMarkdownFence(line);
    if (fence) {
      if (inCode) {
        if (fence.marker === codeFenceMarker) flushCode();
        else codeLines.push(line);
      } else {
        flushParagraph();
        closeList();
        inCode = true;
        codeFenceMarker = fence.marker;
        codeLines = [];
        codeLanguage = normalizeFenceLanguage(fence.info);
      }
      continue;
    }

    if (inCode) {
      codeLines.push(line);
      continue;
    }

    if (!line.trim()) {
      flushParagraph();
      closeList();
      continue;
    }

    if (isInternalTaskJsonText(line)) {
      flushParagraph();
      closeList();
      html += renderInternalTaskJsonBlock(line);
      continue;
    }

    const nextLine = lines[index + 1] || '';
    if (isMarkdownTableHeader(line, nextLine)) {
      flushParagraph();
      closeList();
      const headers = splitMarkdownTableRow(line);
      const alignments = splitMarkdownTableRow(nextLine).map(markdownTableAlignment);
      const rows: string[][] = [];
      index += 2;
      while (index < lines.length && lineLooksLikeMarkdownTableRow(lines[index])) {
        rows.push(splitMarkdownTableRow(lines[index]));
        index += 1;
      }
      index -= 1;
      html += renderMarkdownTable(headers, alignments, rows);
      continue;
    }

    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      closeList();
      const level = heading[1].length;
      html += `<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`;
      continue;
    }

    const quote = line.match(/^>\s?(.*)$/);
    if (quote) {
      flushParagraph();
      closeList();
      html += `<blockquote>${renderInlineMarkdown(quote[1])}</blockquote>`;
      continue;
    }

    const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
    if (unordered) {
      flushParagraph();
      openList('ul');
      html += `<li>${renderInlineMarkdown(unordered[1])}</li>`;
      continue;
    }

    const ordered = line.match(/^\s*\d+\.\s+(.+)$/);
    if (ordered) {
      flushParagraph();
      openList('ol');
      html += `<li>${renderInlineMarkdown(ordered[1])}</li>`;
      continue;
    }

    closeList();
    paragraph.push(line);
  }

  if (inCode) flushCode();
  flushParagraph();
  closeList();
  return html;
}

function escapeHtml(text: string) {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

export function codeBlockStateKey(messageId: string, blockIndex: string) {
  return `${messageId || 'message'}:${blockIndex}`;
}

function renderCodeBlockHtml(
  rawCode: string,
  rawLanguage: string,
  messageId: string,
  copiedCodeBlockKey: string,
  blockIndex: number,
) {
  const normalizedBlock = normalizeCodeBlockContent(rawCode, rawLanguage);
  const code = normalizedBlock.code;
  const language = normalizedBlock.language || detectCodeLanguage(code);
  const blockKey = String(blockIndex);
  const copied = copiedCodeBlockKey === codeBlockStateKey(messageId, blockKey);
  const languageLabel = language ? `<span class="markdown-code-lang">${escapeHtml(language)}</span>` : '<span class="markdown-code-lang">text</span>';
  const copyButtonLabel = copied ? '已复制' : '复制代码';
  const copyButtonIcon = copied ? CODE_CHECK_ICON_HTML : CODE_COPY_ICON_HTML;
  const blockClass = `markdown-code-block${language ? ` markdown-code-block-${escapeHtml(language)}` : ''}`;
  return `<div class="${blockClass}" data-code-index="${blockKey}">${languageLabel}<button type="button" class="markdown-code-copy${copied ? ' copied' : ''}" data-code-copy data-testid="chat-code-copy" aria-label="${copyButtonLabel}" title="${copyButtonLabel}">${copyButtonIcon}</button><pre><code class="${language ? `language-${escapeHtml(language)}` : ''}">${renderHighlightedCode(code, language)}</code></pre></div>`;
}

function isInternalTaskJsonText(value: string) {
  const text = String(value || '').trim();
  if (!text) return false;
  const compact = text.toLowerCase().replace(/[\s_"'`.-]+/g, '');
  if (
    !compact.includes('dispatchgroupagent')
    && !compact.includes('runohaagent')
    && !compact.includes('ohagroupdispatch')
    && !compact.includes('nativegroupdispatch')
  ) {
    return false;
  }
  return text.startsWith('{') || text.startsWith('[') || text.startsWith('<');
}

function renderInternalTaskJsonBlock(value: string) {
  const raw = String(value || '').trim();
  let display = raw;
  try {
    display = JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    // Keep model output readable even when it is a partial or non-standard JSON fragment.
  }
  const preview = raw.replace(/\s+/g, ' ');
  return (
    '<details class="markdown-internal-task-json">'
    + '<summary>'
    + '<span class="markdown-internal-task-json-label">内部任务 JSON</span>'
    + `<code class="markdown-internal-task-json-preview">${escapeHtml(preview)}</code>`
    + '</summary>'
    + `<pre><code>${escapeHtml(display)}</code></pre>`
    + '</details>'
  );
}

function detectStandaloneCodeBlock(source: string): { code: string; language: string } | null {
  if (source.includes('```') || source.includes('~~~')) return null;
  const trimmed = source.trim();
  if (!trimmed) return null;
  if (looksLikeResumeDiffTranscript(trimmed) || looksLikeUnifiedDiff(trimmed)) {
    return { code: trimmed, language: 'diff' };
  }
  return null;
}

function looksLikeResumeDiffTranscript(text: string) {
  return /resumed session/i.test(text)
    && /review\s+diff/i.test(text)
    && looksLikeUnifiedDiff(text);
}

function looksLikeUnifiedDiff(text: string) {
  const hasHunk = /(?:^|\n)@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@/.test(text);
  const hasFileHeader = /(?:^|\n)(?:diff --git|---\s+\S+\n\+\+\+\s+\S+)/.test(text);
  // A valid hunk may only add or only remove lines, including empty lines.
  // Within a hunk, even +++ / --- can be changed content rather than headers.
  let inHunk = false;
  const hasChangedLines = text.split('\n').some((line) => {
    if (/^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@/.test(line)) {
      inHunk = true;
      return false;
    }
    if (line.startsWith('diff --git ')) inHunk = false;
    return /^[+-]/.test(line) && (inHunk || !/^(?:---|\+\+\+)\s/.test(line));
  });
  return (hasHunk || hasFileHeader) && hasChangedLines;
}

function parseMarkdownFence(line: string) {
  const match = line.trim().match(/^(```|~~~|:::)\s*(.*)$/);
  if (!match) return null;
  return {
    marker: match[1],
    info: match[2] || '',
  };
}

function normalizeFenceLanguage(fenceInfo: string) {
  const raw = fenceInfo.trim();
  const lower = raw.toLowerCase();
  if (lower === 'review diff' || lower === 'review-diff' || lower.startsWith('review diff ')) return 'diff';
  if (lower === 'patch' || lower.includes(' diff')) return 'diff';
  return normalizeCodeLanguage(raw.split(/\s+/)[0] || '');
}

function normalizeCodeBlockContent(code: string, language: string) {
  const directLanguage = normalizeCodeLanguage(language);
  const unwrapped = unwrapReviewDiffFence(code);
  if (unwrapped) {
    return {
      code: unwrapped,
      language: 'diff',
    };
  }
  return {
    code,
    language: directLanguage,
  };
}

function unwrapReviewDiffFence(code: string) {
  const lines = String(code || '').replace(/\r\n/g, '\n').split('\n');
  while (lines.length && !lines[0].trim()) lines.shift();
  while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
  if (lines.length < 2) return '';
  const opening = parseMarkdownFence(lines[0]);
  if (!opening) return '';
  if (normalizeFenceLanguage(opening.info) !== 'diff') return '';
  const closing = parseMarkdownFence(lines[lines.length - 1]);
  const contentLines = closing && closing.marker === opening.marker
    ? lines.slice(1, -1)
    : lines.slice(1);
  return contentLines.join('\n');
}

function normalizeCodeLanguage(language: string) {
  const value = String(language || '').trim().toLowerCase().replace(/[^a-z0-9+#.-]/g, '');
  const aliases: Record<string, string> = {
    cjs: 'javascript',
    js: 'javascript',
    jsx: 'javascript',
    mjs: 'javascript',
    node: 'javascript',
    ts: 'typescript',
    tsx: 'typescript',
    py: 'python',
    sh: 'bash',
    shell: 'bash',
    zsh: 'bash',
    yml: 'yaml',
  };
  return aliases[value] || value;
}

function detectCodeLanguage(code: string) {
  const trimmed = code.trim();
  if (!trimmed) return '';
  if ((trimmed.startsWith('{') || trimmed.startsWith('['))) {
    try {
      JSON.parse(trimmed);
      return 'json';
    } catch {
      // Keep looking for a better lightweight guess.
    }
  }
  if (/^@@\s|(?:^|\n)\+[^+\n]/.test(trimmed) && /(?:^|\n)-[^-\n]/.test(trimmed)) return 'diff';
  if (/\bfunc\s+\w+\s*\(|\bpackage\s+main\b|:=/.test(trimmed)) return 'go';
  if (/\b(def|class|from|import)\s+\w+|__name__/.test(trimmed)) return 'python';
  if (/\b(const|let|var|function|interface|type)\s+\w+|=>/.test(trimmed)) return 'typescript';
  if (/^\s*(#!|npm\s|pnpm\s|yarn\s|curl\s|git\s)/m.test(trimmed)) return 'bash';
  if (/^\s*[\w.-]+\s*:\s+\S/m.test(trimmed)) return 'yaml';
  return '';
}

function renderHighlightedCode(code: string, language: string) {
  const normalizedLanguage = normalizeCodeLanguage(language) || detectCodeLanguage(code);
  if (normalizedLanguage === 'diff') return renderDiffCode(code);
  const keywords = codeKeywordsForLanguage(normalizedLanguage);
  let html = '';
  let index = 0;

  while (index < code.length) {
    const char = code[index];
    const next = code[index + 1];

    if (char === '/' && next === '*') {
      const end = code.indexOf('*/', index + 2);
      const stop = end >= 0 ? end + 2 : code.length;
      html += syntaxSpan('comment', code.slice(index, stop));
      index = stop;
      continue;
    }

    if (char === '/' && next === '/') {
      const end = code.indexOf('\n', index + 2);
      const stop = end >= 0 ? end : code.length;
      html += syntaxSpan('comment', code.slice(index, stop));
      index = stop;
      continue;
    }

    if (char === '#' && (normalizedLanguage === 'python' || normalizedLanguage === 'bash' || normalizedLanguage === 'yaml')) {
      const end = code.indexOf('\n', index + 1);
      const stop = end >= 0 ? end : code.length;
      html += syntaxSpan('comment', code.slice(index, stop));
      index = stop;
      continue;
    }

    if (char === '"' || char === "'" || char === '`') {
      const stop = quotedStringEnd(code, index, char);
      const token = code.slice(index, stop);
      const after = nextNonWhitespaceIndex(code, stop);
      const className = code[after] === ':' ? 'property' : 'string';
      html += syntaxSpan(className, token);
      index = stop;
      continue;
    }

    const numberMatch = code.slice(index).match(/^(?:0x[\da-fA-F]+|\d+(?:\.\d+)?(?:e[+-]?\d+)?)/);
    if (numberMatch) {
      html += syntaxSpan('number', numberMatch[0]);
      index += numberMatch[0].length;
      continue;
    }

    const identifierMatch = code.slice(index).match(/^[A-Za-z_$][\w$]*/);
    if (identifierMatch) {
      const word = identifierMatch[0];
      const after = nextNonWhitespaceIndex(code, index + word.length);
      if (keywords.has(word)) {
        html += syntaxSpan('keyword', word);
      } else if (code[after] === '(') {
        html += syntaxSpan('function', word);
      } else {
        html += escapeHtml(word);
      }
      index += word.length;
      continue;
    }

    if (/[\[\]{}().,:;]/.test(char)) {
      html += syntaxSpan('punctuation', char);
      index += 1;
      continue;
    }

    html += escapeHtml(char);
    index += 1;
  }

  return html;
}

function renderDiffCode(code: string) {
  const lines = String(code || '').replace(/\r\n/g, '\n').split('\n');
  return lines.map((line) => {
    const kind = diffLineKind(line);
    const marker = diffLineMarker(line, kind);
    const content = kind === 'add' || kind === 'delete' ? line.slice(1) : line;
    return `<span class="diff-line diff-line-${kind}"><span class="diff-marker">${escapeHtml(marker)}</span><span class="diff-content">${escapeHtml(content || ' ')}</span></span>`;
  }).join('');
}

function diffLineKind(line: string) {
  if (line.startsWith('@@')) return 'hunk';
  if (line.startsWith('+++') || line.startsWith('---')) return 'file';
  if (line.startsWith('+')) return 'add';
  if (line.startsWith('-')) return 'delete';
  return 'context';
}

function diffLineMarker(line: string, kind: string) {
  if (kind === 'add') return '+';
  if (kind === 'delete') return '-';
  return line.startsWith(' ') ? ' ' : '';
}

function codeKeywordsForLanguage(language: string) {
  const common = ['false', 'null', 'true'];
  const byLanguage: Record<string, string[]> = {
    bash: ['case', 'do', 'done', 'elif', 'else', 'esac', 'export', 'fi', 'for', 'function', 'if', 'in', 'local', 'then', 'while'],
    go: ['break', 'case', 'chan', 'const', 'continue', 'defer', 'default', 'else', 'fallthrough', 'for', 'func', 'go', 'if', 'import', 'interface', 'map', 'nil', 'package', 'range', 'return', 'select', 'struct', 'switch', 'type', 'var'],
    javascript: ['async', 'await', 'break', 'case', 'catch', 'class', 'const', 'continue', 'default', 'else', 'export', 'extends', 'finally', 'for', 'from', 'function', 'if', 'import', 'let', 'new', 'return', 'switch', 'this', 'throw', 'try', 'typeof', 'var', 'while'],
    json: common,
    python: ['and', 'as', 'async', 'await', 'break', 'class', 'continue', 'def', 'elif', 'else', 'except', 'False', 'finally', 'for', 'from', 'if', 'import', 'in', 'is', 'None', 'not', 'or', 'pass', 'return', 'True', 'try', 'while', 'with', 'yield'],
    typescript: ['async', 'await', 'break', 'case', 'catch', 'class', 'const', 'continue', 'default', 'else', 'export', 'extends', 'finally', 'for', 'from', 'function', 'if', 'implements', 'import', 'interface', 'let', 'new', 'private', 'protected', 'public', 'readonly', 'return', 'switch', 'this', 'throw', 'try', 'type', 'typeof', 'var', 'while'],
    yaml: ['false', 'null', 'true'],
  };
  return new Set([...(byLanguage[language] || []), ...common]);
}

function quotedStringEnd(code: string, start: number, quote: string) {
  let index = start + 1;
  while (index < code.length) {
    const char = code[index];
    if (char === '\\') {
      index += 2;
      continue;
    }
    if (char === quote) return index + 1;
    index += 1;
  }
  return code.length;
}

function nextNonWhitespaceIndex(code: string, start: number) {
  let index = start;
  while (index < code.length && /\s/.test(code[index])) index += 1;
  return index;
}

function syntaxSpan(kind: string, value: string) {
  return `<span class="syntax-${kind}">${escapeHtml(value)}</span>`;
}

function isMarkdownTableHeader(headerLine: string, separatorLine: string) {
  const headerCells = splitMarkdownTableRow(headerLine);
  if (headerCells.length < 2) return false;
  return isMarkdownTableSeparator(separatorLine, headerCells.length);
}

function isMarkdownTableSeparator(line: string, expectedCells: number) {
  const cells = splitMarkdownTableRow(line);
  if (cells.length < 2 || cells.length < expectedCells) return false;
  return cells.every((cell) => /^:?-{3,}:?$/.test(cell.replace(/\s+/g, '')));
}

function lineLooksLikeMarkdownTableRow(line: string) {
  if (!line.trim()) return false;
  return splitMarkdownTableRow(line).length >= 2;
}

function splitMarkdownTableRow(line: string) {
  let value = line.trim();
  if (value.startsWith('|')) value = value.slice(1);
  if (value.endsWith('|')) value = value.slice(0, -1);
  const cells: string[] = [];
  let current = '';
  let inCode = false;
  for (let index = 0; index < value.length; index += 1) {
    const char = value[index];
    const previous = value[index - 1];
    if (char === '`' && previous !== '\\') inCode = !inCode;
    if (char === '|' && previous !== '\\' && !inCode) {
      cells.push(current.trim().replace(/\\\|/g, '|'));
      current = '';
      continue;
    }
    current += char;
  }
  cells.push(current.trim().replace(/\\\|/g, '|'));
  return cells;
}

function markdownTableAlignment(cell: string): '' | 'left' | 'center' | 'right' {
  const value = cell.replace(/\s+/g, '');
  if (value.startsWith(':') && value.endsWith(':')) return 'center';
  if (value.endsWith(':')) return 'right';
  if (value.startsWith(':')) return 'left';
  return '';
}

function renderMarkdownTable(headers: string[], alignments: Array<'' | 'left' | 'center' | 'right'>, rows: string[][]) {
  const columnCount = headers.length;
  const alignAttr = (index: number) => (alignments[index] ? ` class="align-${alignments[index]}"` : '');
  const headerHtml = headers
    .map((cell, index) => `<th${alignAttr(index)}>${renderInlineMarkdown(cell)}</th>`)
    .join('');
  const bodyHtml = rows
    .map((row) => {
      const cells = Array.from({ length: columnCount }, (_unused, index) => row[index] || '');
      return `<tr>${cells.map((cell, index) => `<td${alignAttr(index)}>${renderInlineMarkdown(cell)}</td>`).join('')}</tr>`;
    })
    .join('');
  return `<div class="markdown-table-wrap"><table><thead><tr>${headerHtml}</tr></thead><tbody>${bodyHtml}</tbody></table></div>`;
}

function renderInlineMarkdown(text: string) {
  const codes: string[] = [];
  let value = escapeHtml(text);
  value = value.replace(/`([^`]+)`/g, (_match, code: string) => {
    const token = `\u0000CODE${codes.length}\u0000`;
    codes.push(`<code>${escapeHtml(code)}</code>`);
    return token;
  });
  value = value.replace(/\[([^\]]+)]\(([^)\s]+)\)/g, (_match, label: string, url: string) => {
    const safeUrl = sanitizeMarkdownUrl(url);
    if (!safeUrl) return escapeHtml(label);
    return `<a href="${safeUrl}" target="_blank" rel="noreferrer">${escapeHtml(label)}</a>`;
  });
  value = value.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  value = value.replace(/__([^_]+)__/g, '<strong>$1</strong>');
  value = value.replace(/(^|[^*])\*([^*\s][^*]*?)\*/g, '$1<em>$2</em>');
  value = value.replace(/(^|[^_])_([^_\s][^_]*?)_/g, '$1<em>$2</em>');
  value = renderMentionTokens(value);
  codes.forEach((code, index) => {
    value = value.replace(`\u0000CODE${index}\u0000`, code);
  });
  return value;
}

function renderMentionTokens(value: string) {
  return value.replace(
    /(^|[\s，。！？、；;,.!?])@(&quot;[^&]+&quot;|'[^']+'|[A-Za-z0-9_\-\u4e00-\u9fff.]+)/g,
    (_match, prefix: string, mention: string) => `${prefix}<span class="mention-token">@${mention}</span>`,
  );
}

function sanitizeMarkdownUrl(url: string) {
  const value = String(url || '').trim();
  if (!value) return '';
  try {
    const parsed = new URL(value);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:' || parsed.protocol === 'mailto:') {
      return escapeHtml(value);
    }
  } catch {
    return '';
  }
  return '';
}
