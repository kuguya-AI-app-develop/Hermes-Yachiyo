#!/usr/bin/env node
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const FRONTEND = path.join(ROOT, 'apps', 'frontend');
const ELECTRON = path.join(FRONTEND, 'node_modules', '.bin', process.platform === 'win32' ? 'electron.cmd' : 'electron');
const VITE = path.join(FRONTEND, 'node_modules', '.bin', process.platform === 'win32' ? 'vite.cmd' : 'vite');

const GROUP_SESSION_ID = 'launcher_group_summary';
const DELEGATED_SESSION_ID = 'launcher_delegated_summary';
const GROUP_TASK_ID = 'launcher_group_summary_task';
const DELEGATED_TASK_ID = 'launcher_delegated_summary_task';
const PUBLIC_TASK_ID = 'launcher_public_agent_task';
const PUBLIC_RUN_ID = 'launcher_public_agent_run';
const PUBLIC_TASK_TITLE = 'Public launcher task from Yachiyo facade';
const BUBBLE_SUMMARY = 'Group summary: Design and Coding finished Native dispatch.';
const DELEGATED_SUMMARY = 'Delegated summary: Coding finished Native delegated run.';
const BUBBLE_QUICK_TEXT = 'Bubble quick input from launcher smoke';
const BUBBLE_TASK_TEXT = 'Bubble delegated task from launcher smoke';
const LIVE2D_REPLY = 'Live2D latest reply from launcher smoke';
const LIVE2D_QUICK_TEXT = 'Live2D quick input from launcher smoke';
const LIVE2D_TASK_TEXT = 'Live2D delegated task from launcher smoke';
const STATUS_LABEL = '2 recent sessions';
const now = new Date().toISOString();

const bridgeState = {
  ackPayloads: [],
  approvalPayloads: [],
  bubbleDefaultOpenBehavior: 'reply_bubble',
  cancelPayloads: [],
  live2dClickAction: 'toggle_reply',
  modeRequests: [],
  publicTaskDecision: '',
  readinessBlocked: false,
  taskRequests: [],
  taskStartPayloads: [],
  quickMessagePayload: null,
  quickMessagePayloads: [],
};

const recentSessions = [
  {
    session_id: GROUP_SESSION_ID,
    conversation_kind: 'group',
    title: 'Launcher Group',
    summary: BUBBLE_SUMMARY,
    latest_task_id: GROUP_TASK_ID,
    latest_status: 'completed',
    updated_at: now,
  },
  {
    session_id: DELEGATED_SESSION_ID,
    conversation_kind: 'agent',
    title: 'Delegated Agent',
    summary: DELEGATED_SUMMARY,
    latest_task_id: DELEGATED_TASK_ID,
    latest_status: 'completed',
    updated_at: now,
  },
];

const previewSvg = Buffer.from(
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 180 240"><rect width="180" height="240" fill="#f4f7fb"/><circle cx="90" cy="80" r="42" fill="#7aa7c7"/><path d="M38 206c12-48 92-48 104 0" fill="#355a7a"/><circle cx="76" cy="75" r="5" fill="#102236"/><circle cx="104" cy="75" r="5" fill="#102236"/><path d="M75 103q15 12 30 0" fill="none" stroke="#102236" stroke-width="6" stroke-linecap="round"/></svg>',
).toString('base64');

function log(message) {
  process.stdout.write(`[launcher-session-summary-ui-smoke] ${message}\n`);
}

function launcherPayload(mode) {
  const live2d = mode === 'live2d';
  return {
    ok: true,
    mode,
    chat: {
      session_id: live2d ? DELEGATED_SESSION_ID : GROUP_SESSION_ID,
      is_processing: false,
      empty: false,
      status_label: STATUS_LABEL,
      latest_reply: live2d ? LIVE2D_REPLY : '',
      latest_reply_full: live2d ? LIVE2D_REPLY : '',
      agent_task: publicAgentTasks()[0],
      recent_sessions: recentSessions,
    },
    notification: {
      has_unread: true,
      latest_message: live2d ? { status: 'completed', content: LIVE2D_REPLY } : { status: 'completed', content: '' },
    },
    proactive: {
      enabled: true,
      has_attention: false,
      message: '',
    },
    launcher: live2d
      ? {
          latest_status: 'completed',
          latest_reply: LIVE2D_REPLY,
          latest_reply_full: LIVE2D_REPLY,
          show_reply_bubble: true,
          enable_quick_input: true,
          click_action: bridgeState.live2dClickAction,
          default_open_behavior: 'chat_input',
          position_anchor: 'bottom-right',
          preview_url: `data:image/svg+xml;base64,${previewSvg}`,
          scale: 1,
          mouse_follow_enabled: false,
          render_quality_preset: 'low',
          renderer: {
            enabled: false,
            reason: 'Smoke preview fallback',
          },
          resource: {
            available: true,
            state: 'loaded',
            display_name: 'Smoke Live2D',
            status_label: 'Smoke Live2D ready',
            help_text: '',
          },
        }
      : {
          default_display: 'summary',
          default_open_behavior: bridgeState.bubbleDefaultOpenBehavior,
          enable_quick_input: true,
          latest_status: 'completed',
          show_unread_dot: true,
          has_attention: false,
          auto_hide: false,
          opacity: 1,
          avatar_url: '',
        },
  };
}

function publicAgentTasks() {
  const running = bridgeState.publicTaskDecision === 'running';
  const staleCompleted = bridgeState.publicTaskDecision === 'approved';
  const resolved = bridgeState.publicTaskDecision === 'approved'
    || bridgeState.publicTaskDecision === 'rejected'
    || bridgeState.publicTaskDecision === 'cancelled';
  return [
    {
      task_id: PUBLIC_TASK_ID,
      conversation_id: DELEGATED_SESSION_ID,
      title: PUBLIC_TASK_TITLE,
      status: bridgeState.publicTaskDecision === 'cancelled'
        ? 'cancelled'
        : bridgeState.publicTaskDecision === 'rejected' ? 'failed' : running ? 'running' : resolved ? 'completed' : 'waiting_approval',
      summary: resolved ? `Launcher task ${bridgeState.publicTaskDecision}` : 'Launcher can observe public Yachiyo task snapshots.',
      current_step: running ? 'tool_loop_internal_step' : resolved ? 'Launcher smoke approval resolved' : 'Awaiting launcher smoke approval',
      progress_text: running ? 'planner replan verification loop' : resolved ? 'Approval resolved' : 'Waiting for approval',
      needs_user_action: staleCompleted || (!resolved && !running),
      pending_approvals: staleCompleted ? [
        {
          approval_id: 'launcher-stale-completed-approval',
          run_id: PUBLIC_RUN_ID,
          title: 'Stale completed approval must stay hidden',
          tool_name: 'workspace.write_patch',
          risk_level: 'high',
          status: 'pending',
        },
      ] : resolved || running ? [] : [
        {
          approval_id: 'launcher-public-approval',
          run_id: PUBLIC_RUN_ID,
          title: 'Approve launcher smoke task',
          tool_name: 'workspace.write_patch',
          risk_level: 'high',
          status: 'pending',
        },
      ],
      recent_events: running ? [{
        event_id: 'launcher-running-internal-event',
        run_id: PUBLIC_RUN_ID,
        sequence: 1,
        event_type: 'tool.started',
        title: 'media.apple_music_play running',
        detail: 'internal planner verification loop',
      }] : [
        {
          event_id: 'launcher-public-event',
          run_id: PUBLIC_RUN_ID,
          sequence: 1,
          event_type: 'tool.approval_required',
          title: 'Approval required',
          detail: 'Launcher public task smoke',
        },
        {
          event_id: 'launcher-public-permission-event',
          run_id: PUBLIC_RUN_ID,
          sequence: 2,
          event_type: 'agent.tool.failed',
          title: 'Music permission required',
          detail: 'media.apple_music_play',
          payload: {
            result: {
              data: {
                blocking_condition: 'desktop_session_locked',
              },
              permission_error: true,
              permission_targets: ['music_app', 'automation'],
              recovery_actions: [
                {
                  input: {},
                  label: '解锁后重新检查前台窗口',
                  permission_target: 'desktop_session_unlocked',
                  risk_level: 'low',
                  tool: 'desktop.active_window',
                },
              ],
              recovery_hints: ['Open Music once and allow Automation permissions.'],
            },
          },
        },
      ],
      runtime_debug: running ? {
        blocked_tool_call_count: 2,
        failed_verification_count: 1,
        needs_replan: true,
      } : null,
      runtime_execution_envelope: running ? {
        requests: [{
          request_id: 'launcher-running-secret-request',
          tool_name: 'media.apple_music_play',
          risk_level: 'high',
        }],
      } : null,
      task_progress: running ? {
        blocked_todos: 2,
        failed_verification_count: 1,
        needs_replan: true,
        status: 'running',
      } : null,
      replan_recoveries: running ? [{
        request_id: 'launcher-running-secret-replan',
        status: 'pending',
        recovery_actions: [{
          action_id: 'launcher-running-secret-action',
          prompt: 'Retry Apple Music with resolved title',
          tool: 'browser.search',
        }],
      }] : [],
      artifacts: [],
      open_in_studio_url: `#/agents?run_id=${PUBLIC_RUN_ID}`,
      created_at: now,
      updated_at: now,
    },
  ];
}

function pickPort() {
  return new Promise((resolve, reject) => {
    const server = http.createServer();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      server.close(() => {
        if (!address || typeof address === 'string') reject(new Error('could not allocate local port'));
        else resolve(address.port);
      });
    });
  });
}

function readRequestJson(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    request.on('data', (chunk) => chunks.push(chunk));
    request.on('end', () => {
      if (!chunks.length) {
        resolve({});
        return;
      }
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
      } catch (error) {
        reject(error);
      }
    });
    request.on('error', reject);
  });
}

function sendJson(response, statusCode, payload) {
  response.writeHead(statusCode, {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'content-type,x-oha-yachiyo-bridge-token',
    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS,PATCH,DELETE',
    'Content-Type': 'application/json; charset=utf-8',
  });
  response.end(JSON.stringify(payload));
}

async function startMockBridge() {
  const server = http.createServer(async (request, response) => {
    try {
      if (request.method === 'OPTIONS') {
        sendJson(response, 204, {});
        return;
      }
      const url = new URL(request.url || '/', 'http://127.0.0.1');
      if (request.method === 'GET' && url.pathname === '/ui/launcher') {
        const mode = url.searchParams.get('mode') === 'live2d' ? 'live2d' : 'bubble';
        bridgeState.modeRequests.push(mode);
        sendJson(response, 200, launcherPayload(mode));
        return;
      }
      if (request.method === 'GET' && url.pathname === '/yachiyo/readiness') {
        sendJson(response, 200, {
          ok: true,
          capabilities: bridgeState.readinessBlocked
            ? { desktop_execution: { missing_permissions: ['accessibility'] } }
            : {},
        });
        return;
      }
      if (request.method === 'GET' && url.pathname === '/yachiyo/tasks') {
        bridgeState.taskRequests.push(url.search);
        sendJson(response, 200, { ok: true, tasks: publicAgentTasks() });
        return;
      }
      if (request.method === 'POST' && url.pathname === '/yachiyo/tasks') {
        const body = await readRequestJson(request);
        bridgeState.taskStartPayloads.push(body);
        sendJson(response, 200, {
          ...publicAgentTasks()[0],
          task_id: `${PUBLIC_TASK_ID}-${bridgeState.taskStartPayloads.length}`,
          title: String(body.title || body.prompt || PUBLIC_TASK_TITLE),
          summary: 'Launcher desktop task started through Yachiyo facade.',
          status: 'running',
          needs_user_action: false,
          pending_approvals: [],
        });
        return;
      }
      if (
        request.method === 'POST'
        && (
          url.pathname === `/yachiyo/tasks/${PUBLIC_TASK_ID}/approve`
          || url.pathname === `/yachiyo/tasks/${PUBLIC_TASK_ID}/approvals/launcher-public-approval/approve`
        )
      ) {
        const body = await readRequestJson(request);
        bridgeState.publicTaskDecision = 'approved';
        bridgeState.approvalPayloads.push({ action: 'approve', body });
        sendJson(response, 200, publicAgentTasks()[0]);
        return;
      }
      if (
        request.method === 'POST'
        && (
          url.pathname === `/yachiyo/tasks/${PUBLIC_TASK_ID}/reject`
          || url.pathname === `/yachiyo/tasks/${PUBLIC_TASK_ID}/approvals/launcher-public-approval/reject`
        )
      ) {
        const body = await readRequestJson(request);
        bridgeState.publicTaskDecision = 'rejected';
        bridgeState.approvalPayloads.push({ action: 'reject', body });
        sendJson(response, 200, publicAgentTasks()[0]);
        return;
      }
      if (request.method === 'POST' && url.pathname === `/yachiyo/tasks/${PUBLIC_TASK_ID}/cancel`) {
        const body = await readRequestJson(request);
        bridgeState.publicTaskDecision = 'cancelled';
        bridgeState.cancelPayloads.push({ action: 'cancel', body });
        sendJson(response, 200, publicAgentTasks()[0]);
        return;
      }
      if (request.method === 'POST' && url.pathname === '/ui/launcher/ack') {
        const body = await readRequestJson(request);
        bridgeState.ackPayloads.push(body);
        sendJson(response, 200, {
          ok: true,
          session_id: body.mode === 'live2d' ? DELEGATED_SESSION_ID : GROUP_SESSION_ID,
        });
        return;
      }
      if (request.method === 'POST' && url.pathname === '/ui/launcher/quick-message') {
        const body = await readRequestJson(request);
        bridgeState.quickMessagePayload = body;
        bridgeState.quickMessagePayloads.push(body);
        sendJson(response, 200, { ok: true, task_id: 'launcher-session-summary-quick-message' });
        return;
      }
      if (request.method === 'GET' && url.pathname === '/__smoke/state') {
        sendJson(response, 200, {
          ackPayloads: bridgeState.ackPayloads,
          approvalPayloads: bridgeState.approvalPayloads,
          bubbleDefaultOpenBehavior: bridgeState.bubbleDefaultOpenBehavior,
          cancelPayloads: bridgeState.cancelPayloads,
          live2dClickAction: bridgeState.live2dClickAction,
          modeRequests: bridgeState.modeRequests,
          publicTaskDecision: bridgeState.publicTaskDecision,
          taskRequests: bridgeState.taskRequests,
          taskStartPayloads: bridgeState.taskStartPayloads,
          quickMessagePayload: bridgeState.quickMessagePayload,
          quickMessagePayloads: bridgeState.quickMessagePayloads,
        });
        return;
      }
      if (request.method === 'GET' && url.pathname === '/__smoke/bubble-chat-input') {
        bridgeState.bubbleDefaultOpenBehavior = 'chat_input';
        sendJson(response, 200, { ok: true, bubbleDefaultOpenBehavior: bridgeState.bubbleDefaultOpenBehavior });
        return;
      }
      if (request.method === 'GET' && url.pathname === '/__smoke/live2d-open-chat') {
        bridgeState.live2dClickAction = 'open_chat';
        sendJson(response, 200, { ok: true, live2dClickAction: bridgeState.live2dClickAction });
        return;
      }
      if (request.method === 'GET' && url.pathname === '/__smoke/reset-public-task') {
        bridgeState.publicTaskDecision = '';
        bridgeState.readinessBlocked = false;
        sendJson(response, 200, {
          ok: true,
          publicTaskDecision: bridgeState.publicTaskDecision,
          readinessBlocked: bridgeState.readinessBlocked,
        });
        return;
      }
      if (request.method === 'GET' && url.pathname === '/__smoke/running-public-task') {
        bridgeState.publicTaskDecision = 'running';
        bridgeState.readinessBlocked = false;
        sendJson(response, 200, {
          ok: true,
          publicTaskDecision: bridgeState.publicTaskDecision,
        });
        return;
      }
      if (request.method === 'GET' && url.pathname === '/__smoke/completed-public-task') {
        bridgeState.publicTaskDecision = 'approved';
        bridgeState.readinessBlocked = false;
        sendJson(response, 200, {
          ok: true,
          publicTaskDecision: bridgeState.publicTaskDecision,
        });
        return;
      }
      if (request.method === 'GET' && url.pathname === '/__smoke/completed-readiness') {
        bridgeState.publicTaskDecision = 'approved';
        bridgeState.readinessBlocked = true;
        sendJson(response, 200, {
          ok: true,
          publicTaskDecision: bridgeState.publicTaskDecision,
          readinessBlocked: bridgeState.readinessBlocked,
        });
        return;
      }
      sendJson(response, 404, { ok: false, error: `not found: ${request.method} ${url.pathname}` });
    } catch (error) {
      sendJson(response, 500, { ok: false, error: error instanceof Error ? error.message : String(error) });
    }
  });
  const port = await pickPort();
  await new Promise((resolve, reject) => {
    server.on('error', reject);
    server.listen(port, '127.0.0.1', resolve);
  });
  return { server, url: `http://127.0.0.1:${port}` };
}

function waitForHttp(url, timeoutMs = 15_000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const attempt = () => {
      http.get(url, (response) => {
        response.resume();
        if (response.statusCode && response.statusCode < 500) {
          resolve();
        } else if (Date.now() - started > timeoutMs) {
          reject(new Error(`timed out waiting for ${url}`));
        } else {
          setTimeout(attempt, 250);
        }
      }).on('error', (error) => {
        if (Date.now() - started > timeoutMs) reject(error);
        else setTimeout(attempt, 250);
      });
    };
    attempt();
  });
}

function startVite(port) {
  const child = spawn(VITE, ['--host', '127.0.0.1', '--port', String(port), '--strictPort'], {
    cwd: FRONTEND,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, FORCE_COLOR: '0' },
  });
  child.stdout.on('data', (chunk) => process.stdout.write(chunk));
  child.stderr.on('data', (chunk) => process.stderr.write(chunk));
  return child;
}

function killProcess(child) {
  if (!child || child.killed) return;
  child.kill('SIGTERM');
}

function runElectronSmoke(devUrl, bridgeUrl) {
  const script = `
const { app, BrowserWindow } = require('electron');
const http = require('node:http');
const devUrl = ${JSON.stringify(devUrl)};
const bridgeUrl = ${JSON.stringify(bridgeUrl)};
const bubbleSummary = ${JSON.stringify(BUBBLE_SUMMARY)};
const delegatedSummary = ${JSON.stringify(DELEGATED_SUMMARY)};
const bubbleQuickText = ${JSON.stringify(BUBBLE_QUICK_TEXT)};
const live2dReply = ${JSON.stringify(LIVE2D_REPLY)};
const live2dQuickText = ${JSON.stringify(LIVE2D_QUICK_TEXT)};
const groupSessionId = ${JSON.stringify(GROUP_SESSION_ID)};
const delegatedSessionId = ${JSON.stringify(DELEGATED_SESSION_ID)};
const groupTaskId = ${JSON.stringify(GROUP_TASK_ID)};
const delegatedTaskId = ${JSON.stringify(DELEGATED_TASK_ID)};
const watchdog = setTimeout(() => {
  console.error('electron smoke timed out');
  app.exit(1);
}, 30000);
function requestBridgeJson(pathname) {
  return new Promise((resolve, reject) => {
    const request = http.request(bridgeUrl + pathname, { method: 'GET' }, (response) => {
      const chunks = [];
      response.on('data', (chunk) => chunks.push(chunk));
      response.on('end', () => {
        const body = Buffer.concat(chunks).toString('utf8');
        if (!response.statusCode || response.statusCode < 200 || response.statusCode >= 300) {
          reject(new Error('GET ' + pathname + ' failed with status ' + response.statusCode + ': ' + body));
          return;
        }
        try {
          resolve(body ? JSON.parse(body) : {});
        } catch (error) {
          reject(error);
        }
      });
    });
    request.on('error', reject);
    request.end();
  });
}
function waitForBridgeState(predicate, label, timeout = 15000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const tick = async () => {
      try {
        const state = await requestBridgeJson('/__smoke/state');
        if (predicate(state)) {
          resolve(state);
          return;
        }
      } catch {}
      if (Date.now() - started > timeout) {
        reject(new Error('timeout waiting for bridge state: ' + label));
      } else {
        setTimeout(tick, 120);
      }
    };
    tick();
  });
}
function waitFor(win, predicate, label, timeout = 15000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const tick = async () => {
      try {
        const result = await win.webContents.executeJavaScript('(' + predicate.toString() + ')()', true);
        if (result) {
          resolve(result);
          return;
        }
      } catch {}
      if (Date.now() - started > timeout) {
        let debug = '';
        try {
          debug = await win.webContents.executeJavaScript(\`
            JSON.stringify({
              location: window.location.href,
              testids: Array.from(document.querySelectorAll('[data-testid]')).slice(0, 80).map((node) => ({
                testid: node.getAttribute('data-testid'),
                hidden: node.hidden,
                session: node.getAttribute('data-session-id'),
                task: node.getAttribute('data-task-id'),
                kind: node.getAttribute('data-conversation-kind'),
                text: (node.textContent || '').slice(0, 240),
              })),
              bodyText: (document.body.textContent || '').slice(-1600),
            })
          \`, true);
        } catch {}
        reject(new Error('timeout waiting for ' + label + (debug ? ': ' + debug : '')));
      } else {
        setTimeout(tick, 120);
      }
    };
    tick();
  });
}
async function installOpenViewProbe(win) {
  await win.webContents.executeJavaScript(\`
    window.__ohaLauncherOpenViewCalls = [];
    window.__ohaLauncherHitRegionCalls = [];
    window.__ohaLauncherTaskRegionRegistered = (testId) => {
      const task = document.querySelector('[data-testid="' + testId + '"]');
      if (!task) return false;
      const bounds = task.getBoundingClientRect();
      return window.__ohaLauncherHitRegionCalls.some((call) => (
        call?.mode === 'live2d'
        && Array.isArray(call?.payload?.regions)
        && call.payload.regions.some((region) => (
          Number(region?.x) <= bounds.left
          && Number(region?.y) <= bounds.top
          && Number(region?.x) + Number(region?.width) >= bounds.right
          && Number(region?.y) + Number(region?.height) >= bounds.bottom
        ))
      ));
    };
    window.ohaDesktop = {
      ...(window.ohaDesktop || {}),
      openView: async (view, params) => {
        window.__ohaLauncherOpenViewCalls.push({ view, params: params || {} });
      },
      setLauncherHitRegions: async (mode, payload) => {
        window.__ohaLauncherHitRegionCalls.push({ mode, payload });
        return true;
      },
    };
    true;
  \`, true);
}
async function assertCompactBubbleLayout(win, size) {
  win.setSize(size, size, false);
  const [contentWidth, contentHeight] = win.getContentSize();
  if (contentWidth > size + 2 || contentHeight > size + 2) {
    throw new Error('compact Bubble viewport size mismatch: ' + JSON.stringify({ contentWidth, contentHeight, size }));
  }
  await waitFor(win, () => Boolean(document.querySelector('[data-testid="bubble-launcher-shell"]')), 'compact bubble viewport ' + size);
  const layout = await win.webContents.executeJavaScript(\`
    (() => {
      const visible = (node) => Boolean(node && node.getClientRects().length && getComputedStyle(node).display !== 'none');
      const inside = (rect) => rect.left >= -0.5 && rect.top >= -0.5 && rect.right <= innerWidth + 0.5 && rect.bottom <= innerHeight + 0.5;
      const button = document.querySelector('[data-testid="bubble-launcher-button"]');
      const indicator = document.querySelector('[data-testid="bubble-launcher-task-status"]');
      const task = document.querySelector('[data-testid="bubble-launcher-agent-task-light"]');
      const readiness = document.querySelector('[data-testid="bubble-launcher-readiness-notice"]');
      const quickInput = document.querySelector('[data-testid="bubble-launcher-quick-input"]');
      const summary = document.querySelector('[data-testid="bubble-launcher-summary"]');
      return {
        buttonInside: Boolean(button && inside(button.getBoundingClientRect())),
        buttonLabel: button?.getAttribute('aria-label') || '',
        indicatorInside: Boolean(indicator && visible(indicator) && inside(indicator.getBoundingClientRect())),
        indicatorText: indicator?.textContent || '',
        quickInputVisible: visible(quickInput),
        readinessVisible: visible(readiness),
        summaryVisible: visible(summary),
        taskText: visible(task) ? task.textContent || '' : '',
        taskVisible: visible(task),
      };
    })()
  \`, true);
  if (!layout.buttonInside || !layout.indicatorInside || layout.taskVisible || layout.readinessVisible || layout.quickInputVisible || layout.summaryVisible) {
    throw new Error('compact Bubble leaked full launcher UI at ' + size + 'px: ' + JSON.stringify(layout));
  }
  if (!/需要你确认|处理中|需要授权|已完成|需要查看|已取消/.test(layout.indicatorText)) {
    throw new Error('compact Bubble missing consumer task status at ' + size + 'px: ' + JSON.stringify(layout));
  }
  if (!layout.buttonLabel.includes(layout.indicatorText)) {
    throw new Error('compact Bubble accessible label missed visible task status at ' + size + 'px: ' + JSON.stringify(layout));
  }
  if (/exec|todo|desktop\.|Agent Studio|runtime/i.test(layout.taskText)) {
    throw new Error('compact Bubble exposed technical runtime text at ' + size + 'px: ' + JSON.stringify(layout));
  }
}
async function main() {
  await app.whenReady();
  console.log('[electron-smoke] app ready');
  const compactWin = new BrowserWindow({
    width: 112,
    height: 112,
    frame: false,
    transparent: true,
    show: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      backgroundThrottling: false,
    },
  });
  await compactWin.loadURL(devUrl + '?bridge=' + encodeURIComponent(bridgeUrl) + '&surface=desktop#/bubble');
  await installOpenViewProbe(compactWin);
  await waitFor(compactWin, () => document.querySelector('[data-testid="bubble-launcher-shell"]'), 'compact bubble shell');
  await waitFor(compactWin, () => document.querySelector('[data-testid="bubble-launcher-agent-task-light"]'), 'compact bubble task fixture');
  await assertCompactBubbleLayout(compactWin, 112);
  await assertCompactBubbleLayout(compactWin, 192);
  await compactWin.webContents.executeJavaScript(
    \`document.querySelector('[data-testid="bubble-launcher-button"]').click()\`,
    true,
  );
  await waitFor(compactWin, () => (
    Array.isArray(window.__ohaLauncherOpenViewCalls)
    && window.__ohaLauncherOpenViewCalls.some((call) => (
      call?.view === 'chat'
      && call?.params?.session_id === ${JSON.stringify(DELEGATED_SESSION_ID)}
      && call?.params?.task_id === ${JSON.stringify(PUBLIC_TASK_ID)}
    ))
  ), 'compact bubble opened matching task chat');
  console.log('[electron-smoke] compact Bubble layout and task-specific Chat handoff verified');
  await requestBridgeJson('/__smoke/completed-readiness');
  const readinessWin = new BrowserWindow({
    width: 112,
    height: 112,
    frame: false,
    transparent: true,
    show: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      backgroundThrottling: false,
    },
  });
  await readinessWin.loadURL(devUrl + '?bridge=' + encodeURIComponent(bridgeUrl) + '&surface=desktop#/bubble');
  await compactWin.close();
  await installOpenViewProbe(readinessWin);
  await waitFor(readinessWin, () => (
    document.querySelector('[data-testid="bubble-launcher-task-status"]')?.textContent?.includes('需要授权')
  ), 'compact bubble completed task with readiness blocker');
  await assertCompactBubbleLayout(readinessWin, 112);
  const readinessLabel = await readinessWin.webContents.executeJavaScript(
    \`document.querySelector('[data-testid="bubble-launcher-button"]')?.getAttribute('aria-label') || ''\`,
    true,
  );
  if (!readinessLabel.includes('需要授权') || !readinessLabel.includes('打开诊断')) {
    throw new Error('compact Bubble readiness label missed status or action: ' + JSON.stringify(readinessLabel));
  }
  await readinessWin.webContents.executeJavaScript(
    \`document.querySelector('[data-testid="bubble-launcher-button"]').click()\`,
    true,
  );
  await waitFor(readinessWin, () => (
    Array.isArray(window.__ohaLauncherOpenViewCalls)
    && window.__ohaLauncherOpenViewCalls.some((call) => (
      call?.view === 'diagnostics'
      && call?.params?.permission_targets === 'accessibility'
    ))
  ), 'compact bubble readiness opened diagnostics');
  await requestBridgeJson('/__smoke/reset-public-task');
  console.log('[electron-smoke] compact Bubble readiness precedence and diagnostics handoff verified');
  await requestBridgeJson('/__smoke/running-public-task');
  const consumerStateWin = new BrowserWindow({
    width: 520,
    height: 420,
    show: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      backgroundThrottling: false,
    },
  });
  await consumerStateWin.loadURL(devUrl + '?bridge=' + encodeURIComponent(bridgeUrl) + '&surface=desktop&smokeReload=consumer-running-bubble#/bubble');
  await readinessWin.close();
  await waitFor(consumerStateWin, () => {
    const summary = document.querySelector('[data-testid="bubble-launcher-summary"]');
    const fullTask = document.querySelector('[data-testid="bubble-launcher-agent-task-light"]');
    const compactTask = document.querySelector('[data-testid="bubble-launcher-agent-task-compact"]');
    const text = document.body.textContent || '';
    return summary?.textContent?.trim() === '处理中'
      && !fullTask
      && !compactTask
      && !/Runtime Debug|Agent Studio|exec|todo|replan|verify|risk|apple_music|browser\.search/i.test(text);
  }, 'bubble running task stays compact and diagnostic-free');
  await consumerStateWin.loadURL(devUrl + '?bridge=' + encodeURIComponent(bridgeUrl) + '&surface=desktop&smokeReload=consumer-running-live2d#/live2d');
  await installOpenViewProbe(consumerStateWin);
  await waitFor(consumerStateWin, () => {
    const compactTask = document.querySelector('[data-testid="live2d-launcher-agent-task-compact"]');
    const fullTask = document.querySelector('[data-testid="live2d-launcher-agent-task-light"]');
    const dots = compactTask?.querySelectorAll('.launcher-agent-task-compact-dots i') || [];
    const text = document.body.textContent || '';
    return compactTask?.textContent?.includes('处理中')
      && dots.length === 3
      && !fullTask
      && !/Runtime Debug|Agent Studio|exec|todo|replan|verify|risk|apple_music|browser\.search/i.test(text);
  }, 'live2d running task uses compact loading cue');
  await waitFor(consumerStateWin, () => (
    window.__ohaLauncherTaskRegionRegistered?.('live2d-launcher-agent-task-compact')
  ), 'live2d compact task registered its desktop hit region');
  await requestBridgeJson('/__smoke/completed-public-task');
  await consumerStateWin.loadURL(devUrl + '?bridge=' + encodeURIComponent(bridgeUrl) + '&surface=desktop&smokeReload=consumer-completed-bubble#/bubble');
  await waitFor(consumerStateWin, () => {
    const taskStatus = document.querySelector('[data-testid="bubble-launcher-task-status"]');
    const summary = document.querySelector('[data-testid="bubble-launcher-summary"]');
    return taskStatus?.textContent?.trim() === '已完成'
      && !document.querySelector('[data-testid="bubble-launcher-agent-task-light"]')
      && !document.querySelector('[data-testid="bubble-launcher-agent-task-compact"]')
      && !summary?.textContent?.includes('需要你确认');
  }, 'completed bubble ignores stale approval flags');
  await consumerStateWin.loadURL(devUrl + '?bridge=' + encodeURIComponent(bridgeUrl) + '&surface=desktop&smokeReload=consumer-completed-live2d#/live2d');
  await waitFor(consumerStateWin, () => (
    !document.querySelector('[data-testid="live2d-launcher-agent-task-light"]')
    && !document.querySelector('[data-testid="live2d-launcher-agent-task-compact"]')
    && document.querySelector('[data-testid="live2d-launcher-latest-reply"]')?.textContent?.includes(${JSON.stringify(LIVE2D_REPLY)})
  ), 'completed launcher task leaves latest reply without task chrome');
  await requestBridgeJson('/__smoke/reset-public-task');
  console.log('[electron-smoke] consumer running/completed Launcher presentation verified');
  const win = new BrowserWindow({
    width: 900,
    height: 700,
    show: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      backgroundThrottling: false,
    },
  });
  // Keep one window alive between phases; Electron otherwise exits while
  // awaiting the mock bridge reset before the next window is constructed.
  await consumerStateWin.close();
  win.webContents.on('console-message', (_event, level, message) => {
    if (level >= 2) console.error('[renderer]', message);
  });
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));

  await win.loadURL(devUrl + '?bridge=' + encodeURIComponent(bridgeUrl) + '&surface=desktop#/bubble');
  await installOpenViewProbe(win);
  console.log('[electron-smoke] bubble loaded');
  await waitFor(win, () => document.querySelector('[data-testid="bubble-launcher-shell"]'), 'bubble shell');
  await waitFor(win, () => {
    const summary = document.querySelector('[data-testid="bubble-launcher-summary"]');
    const probe = document.querySelector('[data-testid="bubble-launcher-session-summary-probe"]');
    const status = document.querySelector('[data-testid="bubble-launcher-status-label"]');
    const taskLight = document.querySelector('[data-testid="bubble-launcher-agent-task-light"]');
    const taskDiagnostics = document.querySelector('[data-testid="bubble-launcher-agent-task-open-diagnostics"]');
    const taskRecovery = document.querySelector('[data-testid="bubble-launcher-agent-task-run-recovery-action"]');
    const taskRuntimeDebug = document.querySelector('[data-testid="bubble-launcher-agent-task-runtime-debug"]');
    const taskPlanner = document.querySelector('[data-testid="bubble-launcher-agent-task-planner-summary"]');
    const taskProgress = document.querySelector('[data-testid="bubble-launcher-agent-task-progress"]');
    const taskApprove = document.querySelector('[data-testid="bubble-launcher-agent-task-approve"]');
    const taskReject = document.querySelector('[data-testid="bubble-launcher-agent-task-reject"]');
    const taskCancel = document.querySelector('[data-testid="bubble-launcher-agent-task-cancel"]');
    const sessions = Array.from(document.querySelectorAll('[data-testid="bubble-launcher-recent-session"]'));
    const bodyText = document.body.textContent || '';
    return summary
      && summary.textContent.includes('需要你确认')
      && probe
      && status?.textContent.includes('2 recent sessions')
      && taskLight
      && taskLight.getAttribute('data-task-id') === ${JSON.stringify(PUBLIC_TASK_ID)}
      && taskLight.getAttribute('data-run-id') === ${JSON.stringify(PUBLIC_RUN_ID)}
      && taskLight.getAttribute('data-presentation-state') === 'approval'
      && taskLight.textContent.includes('需要你的确认')
      && taskLight.textContent.includes('确认后会继续当前任务')
      && !document.querySelector('[data-testid="bubble-launcher-agent-task-open-studio"]')
      && !taskRuntimeDebug
      && !taskPlanner
      && !taskProgress
      && taskDiagnostics?.getAttribute('data-permission-targets') === 'music_app,automation'
      && taskDiagnostics?.getAttribute('data-blocking-conditions') === 'desktop_session_locked'
      && taskDiagnostics?.getAttribute('data-recovery-kind') === 'mixed'
      && taskDiagnostics.textContent.includes('授权')
      && taskRecovery?.getAttribute('data-permission-target') === 'desktop_session_unlocked'
      && taskRecovery?.getAttribute('data-recovery-tool') === 'desktop.active_window'
      && taskRecovery.textContent.includes('重试')
      && !taskRecovery.disabled
      && taskApprove
      && !taskApprove.disabled
      && taskReject
      && !taskReject.disabled
      && taskCancel
      && !taskCancel.disabled
      && sessions.length === 2
      && sessions[0].getAttribute('data-session-id') === ${JSON.stringify(GROUP_SESSION_ID)}
      && sessions[0].getAttribute('data-task-id') === ${JSON.stringify(GROUP_TASK_ID)}
      && sessions[0].getAttribute('data-conversation-kind') === 'group'
      && sessions[0].textContent.includes(${JSON.stringify(BUBBLE_SUMMARY)})
      && sessions[1].getAttribute('data-session-id') === ${JSON.stringify(DELEGATED_SESSION_ID)}
      && sessions[1].getAttribute('data-task-id') === ${JSON.stringify(DELEGATED_TASK_ID)}
      && sessions[1].getAttribute('data-conversation-kind') === 'agent'
      && sessions[1].textContent.includes(${JSON.stringify(DELEGATED_SUMMARY)})
      && !bodyText.includes('oha.group_dispatch')
      && !bodyText.includes('<oha_group_dispatch>')
      && !bodyText.includes('run_oha_agent')
      && !bodyText.includes('<oha_delegation>')
      && !bodyText.includes('Runtime Debug')
      && !bodyText.includes('Agent Studio')
      && !bodyText.includes('workspace.write_patch')
      && !bodyText.includes('media.apple_music_play')
      && !bodyText.includes('desktop.active_window');
  }, 'bubble summary and recent sessions');
  console.log('[electron-smoke] bubble summary rendered');
  console.log('[electron-smoke] bubble consumer task action stays free of runtime diagnostics');
  await win.webContents.executeJavaScript(\`
    (() => {
      const diagnostics = document.querySelector('[data-testid="bubble-launcher-agent-task-open-diagnostics"]');
      if (!diagnostics) throw new Error('missing bubble launcher task diagnostics handoff');
      diagnostics.click();
    })();
  \`, true);
  await waitFor(win, () => (
    Array.isArray(window.__ohaLauncherOpenViewCalls)
    && window.__ohaLauncherOpenViewCalls.some((call) => (
      call?.view === 'diagnostics'
      && call?.params?.command === 'native doctor'
      && call?.params?.blocking_conditions === 'desktop_session_locked'
      && call?.params?.permission_targets === 'music_app,automation'
      && call?.params?.return_to === 'bubble'
    ))
  ), 'bubble launcher task opened Diagnostics');
  console.log('[electron-smoke] bubble launcher task Diagnostics handoff verified');
  await win.webContents.executeJavaScript(\`
    const cancel = document.querySelector('[data-testid="bubble-launcher-agent-task-cancel"]');
    if (!cancel) throw new Error('missing bubble launcher task cancel');
    cancel.click();
  \`, true);
  await waitForBridgeState((state) => (
    Array.isArray(state.cancelPayloads)
    && state.cancelPayloads.some((payload) => payload?.action === 'cancel')
  ), 'bubble launcher task cancel');
  console.log('[electron-smoke] bubble task cancel verified');
  await requestBridgeJson('/__smoke/reset-public-task');
  await win.loadURL(devUrl + '?bridge=' + encodeURIComponent(bridgeUrl) + '&surface=desktop&smokeReload=bubble-task-reset#/bubble');
  await installOpenViewProbe(win);
  await waitFor(win, () => {
    const taskLight = document.querySelector('[data-testid="bubble-launcher-agent-task-light"]');
    const taskApprove = document.querySelector('[data-testid="bubble-launcher-agent-task-approve"]');
    const taskCancel = document.querySelector('[data-testid="bubble-launcher-agent-task-cancel"]');
    return taskLight
      && taskLight.getAttribute('data-task-id') === ${JSON.stringify(PUBLIC_TASK_ID)}
      && taskLight.textContent.includes('需要你的确认')
      && taskApprove
      && !taskApprove.disabled
      && taskCancel
      && !taskCancel.disabled;
  }, 'bubble task restored after cancel smoke');
  await win.webContents.executeJavaScript(\`
    const approve = document.querySelector('[data-testid="bubble-launcher-agent-task-approve"]');
    if (!approve) throw new Error('missing bubble launcher task approve');
    approve.click();
  \`, true);
  await waitForBridgeState((state) => (
    Array.isArray(state.approvalPayloads)
    && state.approvalPayloads.some((payload) => (
      payload?.action === 'approve'
      && payload?.body?.approval_id === 'launcher-public-approval'
    ))
  ), 'bubble launcher task approval');
  console.log('[electron-smoke] bubble task approval verified');
  await win.webContents.executeJavaScript(\`
    const button = document.querySelector('[data-testid="bubble-launcher-button"]');
    if (!button) throw new Error('missing bubble launcher button');
    button.click();
  \`, true);
  await waitForBridgeState((state) => (
    Array.isArray(state.ackPayloads)
    && state.ackPayloads.some((payload) => payload?.mode === 'bubble')
  ), 'bubble launcher ack');
  await waitFor(win, () => (
    Array.isArray(window.__ohaLauncherOpenViewCalls)
    && window.__ohaLauncherOpenViewCalls.some((call) => (
      call?.view === 'chat'
      && call?.params?.session_id === ${JSON.stringify(DELEGATED_SESSION_ID)}
      && call?.params?.task_id === ${JSON.stringify(PUBLIC_TASK_ID)}
    ))
  ), 'bubble launcher opened matching task chat');
  console.log('[electron-smoke] bubble launcher ack verified');

  await requestBridgeJson('/__smoke/bubble-chat-input');
  await win.loadURL(devUrl + '?bridge=' + encodeURIComponent(bridgeUrl) + '&surface=desktop#/bubble');
  await installOpenViewProbe(win);
  console.log('[electron-smoke] bubble chat input loaded');
  await waitFor(win, () => {
    const quickInput = document.querySelector('[data-testid="bubble-launcher-quick-input"]');
    const quickField = document.querySelector('[data-testid="bubble-launcher-quick-input-field"]');
    const quickDelegate = document.querySelector('[data-testid="bubble-launcher-quick-input-delegate"]');
    const quickSubmit = document.querySelector('[data-testid="bubble-launcher-quick-input-submit"]');
    return quickInput && quickField && quickDelegate && quickSubmit;
  }, 'bubble quick input visible');
  await win.webContents.executeJavaScript(\`
    {
      const field = document.querySelector('[data-testid="bubble-launcher-quick-input-field"]');
      if (!field) throw new Error('missing bubble quick input field for task delegation');
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(field, ${JSON.stringify(`  ${BUBBLE_TASK_TEXT}  `)});
      field.dispatchEvent(new Event('input', { bubbles: true }));
    }
  \`, true);
  await waitFor(win, () => {
    const delegate = document.querySelector('[data-testid="bubble-launcher-quick-input-delegate"]');
    return delegate && !delegate.disabled;
  }, 'bubble quick input delegate enabled');
  await win.webContents.executeJavaScript(\`
    (() => {
      const delegate = document.querySelector('[data-testid="bubble-launcher-quick-input-delegate"]');
      if (!delegate) throw new Error('missing bubble quick input delegate');
      delegate.click();
    })();
  \`, true);
  await waitForBridgeState((state) => (
    Array.isArray(state.taskStartPayloads)
    && state.taskStartPayloads.some((payload) => (
      payload?.prompt === ${JSON.stringify(BUBBLE_TASK_TEXT)}
      && payload?.metadata?.source === 'launcher'
      && payload?.metadata?.launcher_mode === 'bubble'
      && payload?.metadata?.launcher_surface === 'desktop_launcher'
    ))
  ), 'bubble launcher delegated task payload');
  await waitFor(win, () => !document.querySelector('[data-testid="bubble-launcher-quick-input"]'), 'bubble delegated task submitted');
  console.log('[electron-smoke] bubble delegated task submitted');

  await win.loadURL(devUrl + '?bridge=' + encodeURIComponent(bridgeUrl) + '&surface=desktop&smokeReload=bubble-task-delegated#/bubble');
  await installOpenViewProbe(win);
  await waitFor(win, () => {
    const quickInput = document.querySelector('[data-testid="bubble-launcher-quick-input"]');
    const quickField = document.querySelector('[data-testid="bubble-launcher-quick-input-field"]');
    const quickSubmit = document.querySelector('[data-testid="bubble-launcher-quick-input-submit"]');
    return quickInput && quickField && quickSubmit;
  }, 'bubble quick input restored after task delegation');
  await win.webContents.executeJavaScript(\`
    {
      const field = document.querySelector('[data-testid="bubble-launcher-quick-input-field"]');
      if (!field) throw new Error('missing bubble quick input field');
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(field, ${JSON.stringify(`  ${BUBBLE_QUICK_TEXT}  `)});
      field.dispatchEvent(new Event('input', { bubbles: true }));
    }
  \`, true);
  await waitFor(win, () => {
    const submit = document.querySelector('[data-testid="bubble-launcher-quick-input-submit"]');
    return submit && !submit.disabled;
  }, 'bubble quick input submit enabled');
  await win.webContents.executeJavaScript(\`
    (() => {
      const submit = document.querySelector('[data-testid="bubble-launcher-quick-input-submit"]');
      if (!submit) throw new Error('missing bubble quick input submit');
      submit.click();
    })();
  \`, true);
  await waitForBridgeState((state) => (
    Array.isArray(state.taskStartPayloads)
    && state.taskStartPayloads.some((payload) => (
      payload?.prompt === ${JSON.stringify(BUBBLE_QUICK_TEXT)}
      && payload?.metadata?.source === 'launcher'
      && payload?.metadata?.launcher_mode === 'bubble'
      && payload?.metadata?.launcher_surface === 'desktop_launcher'
    ))
  ), 'bubble quick input public task payload');
  await waitFor(win, () => !document.querySelector('[data-testid="bubble-launcher-quick-input"]'), 'bubble quick input submitted');
  console.log('[electron-smoke] bubble quick input submitted: ' + bubbleQuickText);

  await requestBridgeJson('/__smoke/reset-public-task');
  await win.loadURL(devUrl + '?bridge=' + encodeURIComponent(bridgeUrl) + '&surface=desktop&smokeReload=live2d-initial#/live2d');
  await installOpenViewProbe(win);
  console.log('[electron-smoke] live2d loaded');
  await waitFor(win, () => document.querySelector('[data-testid="live2d-launcher-shell"]'), 'live2d shell');
  await waitFor(win, () => {
    const quickInput = document.querySelector('[data-testid="live2d-launcher-quick-input"]');
    const quickField = document.querySelector('[data-testid="live2d-launcher-quick-input-field"]');
    const quickSubmit = document.querySelector('[data-testid="live2d-launcher-quick-input-submit"]');
    const latestReply = document.querySelector('[data-testid="live2d-launcher-latest-reply"]');
    const probe = document.querySelector('[data-testid="live2d-launcher-session-summary-probe"]');
    const preview = document.querySelector('[data-testid="live2d-launcher-preview-fallback"]');
    const taskLight = document.querySelector('[data-testid="live2d-launcher-agent-task-light"]');
    const taskDiagnostics = document.querySelector('[data-testid="live2d-launcher-agent-task-open-diagnostics"]');
    const taskRecovery = document.querySelector('[data-testid="live2d-launcher-agent-task-run-recovery-action"]');
    const taskRuntimeDebug = document.querySelector('[data-testid="live2d-launcher-agent-task-runtime-debug"]');
    const taskPlanner = document.querySelector('[data-testid="live2d-launcher-agent-task-planner-summary"]');
    const taskProgress = document.querySelector('[data-testid="live2d-launcher-agent-task-progress"]');
    const taskApprove = document.querySelector('[data-testid="live2d-launcher-agent-task-approve"]');
    const taskReject = document.querySelector('[data-testid="live2d-launcher-agent-task-reject"]');
    const taskCancel = document.querySelector('[data-testid="live2d-launcher-agent-task-cancel"]');
    const sessions = Array.from(document.querySelectorAll('[data-testid="live2d-launcher-recent-session"]'));
    const bodyText = document.body.textContent || '';
    return quickInput
      && quickField
      && quickSubmit
      && latestReply?.textContent.includes(${JSON.stringify(LIVE2D_REPLY)})
      && probe
      && preview
      && taskLight
      && taskLight.getAttribute('data-task-id') === ${JSON.stringify(PUBLIC_TASK_ID)}
      && taskLight.getAttribute('data-run-id') === ${JSON.stringify(PUBLIC_RUN_ID)}
      && taskLight.getAttribute('data-presentation-state') === 'approval'
      && taskLight.textContent.includes('需要你的确认')
      && taskLight.textContent.includes('确认后会继续当前任务')
      && !document.querySelector('[data-testid="live2d-launcher-agent-task-open-studio"]')
      && !taskRuntimeDebug
      && !taskPlanner
      && !taskProgress
      && taskDiagnostics?.getAttribute('data-permission-targets') === 'music_app,automation'
      && taskDiagnostics?.getAttribute('data-blocking-conditions') === 'desktop_session_locked'
      && taskDiagnostics?.getAttribute('data-recovery-kind') === 'mixed'
      && taskDiagnostics.textContent.includes('授权')
      && taskRecovery?.getAttribute('data-permission-target') === 'desktop_session_unlocked'
      && taskRecovery?.getAttribute('data-recovery-tool') === 'desktop.active_window'
      && taskRecovery.textContent.includes('重试')
      && !taskRecovery.disabled
      && taskApprove
      && !taskApprove.disabled
      && taskReject
      && !taskReject.disabled
      && taskCancel
      && !taskCancel.disabled
      && sessions.length === 2
      && sessions[0].getAttribute('data-session-id') === ${JSON.stringify(GROUP_SESSION_ID)}
      && sessions[0].getAttribute('data-task-id') === ${JSON.stringify(GROUP_TASK_ID)}
      && sessions[0].getAttribute('data-conversation-kind') === 'group'
      && sessions[0].textContent.includes(${JSON.stringify(BUBBLE_SUMMARY)})
      && sessions[1].getAttribute('data-session-id') === ${JSON.stringify(DELEGATED_SESSION_ID)}
      && sessions[1].getAttribute('data-task-id') === ${JSON.stringify(DELEGATED_TASK_ID)}
      && sessions[1].getAttribute('data-conversation-kind') === 'agent'
      && sessions[1].textContent.includes(${JSON.stringify(DELEGATED_SUMMARY)})
      && !bodyText.includes('oha.group_dispatch')
      && !bodyText.includes('<oha_group_dispatch>')
      && !bodyText.includes('run_oha_agent')
      && !bodyText.includes('<oha_delegation>')
      && !bodyText.includes('Runtime Debug')
      && !bodyText.includes('Agent Studio')
      && !bodyText.includes('workspace.write_patch')
      && !bodyText.includes('media.apple_music_play')
      && !bodyText.includes('desktop.active_window');
  }, 'live2d quick input and recent sessions');
  console.log('[electron-smoke] live2d summary rendered');
  console.log('[electron-smoke] live2d consumer task action stays free of runtime diagnostics');
  await waitFor(win, () => (
    window.__ohaLauncherTaskRegionRegistered?.('live2d-launcher-agent-task-light')
  ), 'live2d action task registered its desktop hit region');
  await win.webContents.executeJavaScript(\`
    const reject = document.querySelector('[data-testid="live2d-launcher-agent-task-reject"]');
    if (!reject) throw new Error('missing live2d launcher task reject');
    reject.click();
  \`, true);
  await waitForBridgeState((state) => (
    Array.isArray(state.approvalPayloads)
    && state.approvalPayloads.some((payload) => (
      payload?.action === 'reject'
      && payload?.body?.approval_id === 'launcher-public-approval'
    ))
  ), 'live2d launcher task rejection');
  console.log('[electron-smoke] live2d task rejection verified');
  await win.webContents.executeJavaScript(\`
    {
      const field = document.querySelector('[data-testid="live2d-launcher-quick-input-field"]');
      if (!field) throw new Error('missing live2d quick input field for task delegation');
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(field, ${JSON.stringify(`  ${LIVE2D_TASK_TEXT}  `)});
      field.dispatchEvent(new Event('input', { bubbles: true }));
    }
  \`, true);
  await waitFor(win, () => {
    const delegate = document.querySelector('[data-testid="live2d-launcher-quick-input-delegate"]');
    return delegate && !delegate.disabled;
  }, 'live2d quick input delegate enabled');
  await win.webContents.executeJavaScript(\`
    (() => {
      const delegate = document.querySelector('[data-testid="live2d-launcher-quick-input-delegate"]');
      if (!delegate) throw new Error('missing live2d quick input delegate');
      delegate.click();
    })();
  \`, true);
  await waitForBridgeState((state) => (
    Array.isArray(state.taskStartPayloads)
    && state.taskStartPayloads.some((payload) => (
      payload?.prompt === ${JSON.stringify(LIVE2D_TASK_TEXT)}
      && payload?.metadata?.source === 'launcher'
      && payload?.metadata?.launcher_mode === 'live2d'
      && payload?.metadata?.launcher_surface === 'desktop_launcher'
    ))
  ), 'live2d launcher delegated task payload');
  await waitFor(win, () => !document.querySelector('[data-testid="live2d-launcher-quick-input"]'), 'live2d delegated task submitted');
  console.log('[electron-smoke] live2d delegated task submitted');

  await win.loadURL(devUrl + '?bridge=' + encodeURIComponent(bridgeUrl) + '&surface=desktop&smokeReload=live2d-task-delegated#/live2d');
  await installOpenViewProbe(win);
  await waitFor(win, () => {
    const quickInput = document.querySelector('[data-testid="live2d-launcher-quick-input"]');
    const quickField = document.querySelector('[data-testid="live2d-launcher-quick-input-field"]');
    const quickSubmit = document.querySelector('[data-testid="live2d-launcher-quick-input-submit"]');
    return quickInput && quickField && quickSubmit;
  }, 'live2d quick input restored after task delegation');
  await win.webContents.executeJavaScript(\`
    {
      const field = document.querySelector('[data-testid="live2d-launcher-quick-input-field"]');
      if (!field) throw new Error('missing live2d quick input field');
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(field, ${JSON.stringify(`  ${LIVE2D_QUICK_TEXT}  `)});
      field.dispatchEvent(new Event('input', { bubbles: true }));
    }
  \`, true);
  await waitFor(win, () => {
    const submit = document.querySelector('[data-testid="live2d-launcher-quick-input-submit"]');
    return submit && !submit.disabled;
  }, 'live2d quick input submit enabled');
  await win.webContents.executeJavaScript(\`
    (() => {
      const submit = document.querySelector('[data-testid="live2d-launcher-quick-input-submit"]');
      if (!submit) throw new Error('missing live2d quick input submit');
      submit.click();
    })();
  \`, true);
  await waitForBridgeState((state) => (
    Array.isArray(state.taskStartPayloads)
    && state.taskStartPayloads.some((payload) => (
      payload?.prompt === ${JSON.stringify(LIVE2D_QUICK_TEXT)}
      && payload?.metadata?.source === 'launcher'
      && payload?.metadata?.launcher_mode === 'live2d'
      && payload?.metadata?.launcher_surface === 'desktop_launcher'
    ))
  ), 'live2d quick input public task payload');
  await waitFor(win, () => {
    const quickInput = document.querySelector('[data-testid="live2d-launcher-quick-input"]');
    const reply = document.querySelector('[data-testid="live2d-launcher-reply-text"]');
    return !quickInput && reply && reply.textContent.includes(${JSON.stringify(LIVE2D_REPLY)});
  }, 'live2d quick input submitted and reply restored');
  console.log('[electron-smoke] live2d quick input submitted: ' + live2dQuickText);
  await win.webContents.executeJavaScript(\`
    (() => {
      const stage = document.querySelector('[data-testid="live2d-launcher-stage"]');
      if (!stage) throw new Error('missing live2d launcher stage');
      stage.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    })();
  \`, true);
  await waitForBridgeState((state) => (
    Array.isArray(state.ackPayloads)
    && state.ackPayloads.some((payload) => payload?.mode === 'live2d')
  ), 'live2d launcher stage ack');
  console.log('[electron-smoke] live2d launcher ack verified');

  await requestBridgeJson('/__smoke/live2d-open-chat');
  const launcherRequestCountBeforeOpenChat = (await requestBridgeJson('/__smoke/state')).modeRequests.length;
  await win.loadURL(devUrl + '?bridge=' + encodeURIComponent(bridgeUrl) + '&surface=desktop#/live2d');
  await installOpenViewProbe(win);
  await waitFor(win, () => document.querySelector('[data-testid="live2d-launcher-shell"]'), 'live2d open-chat shell');
  await waitForBridgeState((state) => (
    state.live2dClickAction === 'open_chat'
    && Array.isArray(state.modeRequests)
    && state.modeRequests.length > launcherRequestCountBeforeOpenChat
  ), 'live2d open-chat launcher payload');
  const live2dAckCountBeforeOpenChat = (await requestBridgeJson('/__smoke/state')).ackPayloads
    .filter((payload) => payload?.mode === 'live2d').length;
  await win.webContents.executeJavaScript(\`
    (() => {
      const stage = document.querySelector('[data-testid="live2d-launcher-stage"]');
      if (!stage) throw new Error('missing live2d launcher stage for open chat');
      stage.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    })();
  \`, true);
  await waitForBridgeState((state) => (
    Array.isArray(state.ackPayloads)
    && state.ackPayloads.filter((payload) => payload?.mode === 'live2d').length > live2dAckCountBeforeOpenChat
  ), 'live2d open-chat ack');
  await waitFor(win, () => (
    Array.isArray(window.__ohaLauncherOpenViewCalls)
    && window.__ohaLauncherOpenViewCalls.some((call) => (
      call?.view === 'chat'
      && call?.params?.session_id === ${JSON.stringify(DELEGATED_SESSION_ID)}
      && call?.params?.conversation_kind === 'agent'
      && call?.params?.task_id === ${JSON.stringify(DELEGATED_TASK_ID)}
    ))
  ), 'live2d launcher opened delegated chat session');
  console.log('[electron-smoke] live2d open-chat session handoff verified');
  clearTimeout(watchdog);
  await win.close();
  app.quit();
}
main().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  app.exit(1);
});
`;
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'oha-launcher-summary-smoke-'));
  const mainPath = path.join(tempDir, 'main.cjs');
  fs.writeFileSync(mainPath, script, 'utf8');
  return new Promise((resolve, reject) => {
    const child = spawn(ELECTRON, [mainPath], {
      cwd: FRONTEND,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, ELECTRON_ENABLE_LOGGING: '1' },
    });
    const timeout = setTimeout(() => {
      child.kill('SIGTERM');
      reject(new Error('electron smoke child timed out'));
    }, 45_000);
    child.stdout.on('data', (chunk) => process.stdout.write(chunk));
    child.stderr.on('data', (chunk) => process.stderr.write(chunk));
    child.on('error', (error) => {
      clearTimeout(timeout);
      fs.rmSync(tempDir, { recursive: true, force: true });
      reject(error);
    });
    child.on('exit', (code, signal) => {
      clearTimeout(timeout);
      fs.rmSync(tempDir, { recursive: true, force: true });
      if (code === 0) resolve();
      else reject(new Error(`electron smoke failed with code=${code} signal=${signal || ''}`));
    });
  });
}

function assertMockBridgeContract() {
  if (!bridgeState.modeRequests.includes('bubble')) {
    throw new Error('bubble launcher payload was not requested');
  }
  if (!bridgeState.modeRequests.includes('live2d')) {
    throw new Error('live2d launcher payload was not requested');
  }
  if (bridgeState.taskRequests.length) {
    throw new Error(`launcher idle refresh must not list all Yachiyo tasks: ${JSON.stringify(bridgeState.taskRequests)}`);
  }
  const taskStartPayloads = bridgeState.taskStartPayloads;
  const bubbleTaskPayload = taskStartPayloads.find((payload) => (
    payload?.prompt === BUBBLE_TASK_TEXT
    && payload?.metadata?.launcher_mode === 'bubble'
  ));
  if (!bubbleTaskPayload) {
    throw new Error(`bubble delegated task did not call /yachiyo/tasks: ${JSON.stringify(taskStartPayloads)}`);
  }
  if (
    bubbleTaskPayload.prompt !== BUBBLE_TASK_TEXT
    || bubbleTaskPayload.metadata?.source !== 'launcher'
    || bubbleTaskPayload.metadata?.launcher_surface !== 'desktop_launcher'
  ) {
    throw new Error(`bubble delegated task metadata mismatch: ${JSON.stringify(bubbleTaskPayload)}`);
  }
  const bubbleQuickTaskPayload = taskStartPayloads.find((payload) => (
    payload?.prompt === BUBBLE_QUICK_TEXT
    && payload?.metadata?.launcher_mode === 'bubble'
  ));
  if (!bubbleQuickTaskPayload) {
    throw new Error(`bubble quick input did not call /yachiyo/tasks: ${JSON.stringify(taskStartPayloads)}`);
  }
  if (
    bubbleQuickTaskPayload.metadata?.source !== 'launcher'
    || bubbleQuickTaskPayload.metadata?.launcher_surface !== 'desktop_launcher'
  ) {
    throw new Error(`bubble quick input task metadata mismatch: ${JSON.stringify(bubbleQuickTaskPayload)}`);
  }
  const live2dTaskPayload = taskStartPayloads.find((payload) => (
    payload?.prompt === LIVE2D_TASK_TEXT
    && payload?.metadata?.launcher_mode === 'live2d'
  ));
  if (!live2dTaskPayload) {
    throw new Error(`live2d delegated task did not call /yachiyo/tasks: ${JSON.stringify(taskStartPayloads)}`);
  }
  if (
    live2dTaskPayload.prompt !== LIVE2D_TASK_TEXT
    || live2dTaskPayload.metadata?.source !== 'launcher'
    || live2dTaskPayload.metadata?.launcher_surface !== 'desktop_launcher'
  ) {
    throw new Error(`live2d delegated task metadata mismatch: ${JSON.stringify(live2dTaskPayload)}`);
  }
  const live2dQuickTaskPayload = taskStartPayloads.find((payload) => (
    payload?.prompt === LIVE2D_QUICK_TEXT
    && payload?.metadata?.launcher_mode === 'live2d'
  ));
  if (!live2dQuickTaskPayload) {
    throw new Error(`live2d quick input did not call /yachiyo/tasks: ${JSON.stringify(taskStartPayloads)}`);
  }
  if (
    live2dQuickTaskPayload.metadata?.source !== 'launcher'
    || live2dQuickTaskPayload.metadata?.launcher_surface !== 'desktop_launcher'
  ) {
    throw new Error(`live2d quick input task metadata mismatch: ${JSON.stringify(live2dQuickTaskPayload)}`);
  }
  const approvalActions = bridgeState.approvalPayloads.map((payload) => payload?.action);
  if (!approvalActions.includes('approve')) {
    throw new Error(`bubble launcher task approval was not called: ${JSON.stringify(bridgeState.approvalPayloads)}`);
  }
  if (!approvalActions.includes('reject')) {
    throw new Error(`live2d launcher task rejection was not called: ${JSON.stringify(bridgeState.approvalPayloads)}`);
  }
  if (!bridgeState.cancelPayloads.some((payload) => payload?.action === 'cancel')) {
    throw new Error(`bubble launcher task cancel was not called: ${JSON.stringify(bridgeState.cancelPayloads)}`);
  }
  const ackModes = bridgeState.ackPayloads.map((payload) => payload?.mode);
  if (!ackModes.includes('bubble')) {
    throw new Error(`bubble launcher ack was not called: ${JSON.stringify(bridgeState.ackPayloads)}`);
  }
  if (!ackModes.includes('live2d')) {
    throw new Error(`live2d launcher ack was not called: ${JSON.stringify(bridgeState.ackPayloads)}`);
  }
  if (bridgeState.quickMessagePayloads.length) {
    throw new Error(`launcher quick input should prefer /yachiyo/tasks before quick-message fallback: ${JSON.stringify(bridgeState.quickMessagePayloads)}`);
  }
}

async function main() {
  const bridge = await startMockBridge();
  const vitePort = await pickPort();
  const vite = startVite(vitePort);
  try {
    const devUrl = `http://127.0.0.1:${vitePort}`;
    await waitForHttp(devUrl);
    await runElectronSmoke(devUrl, bridge.url);
    assertMockBridgeContract();
    log('passed');
  } finally {
    killProcess(vite);
    await new Promise((resolve) => bridge.server.close(resolve));
  }
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
