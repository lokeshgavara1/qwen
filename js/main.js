// App bootstrap: wires up DOM, settings, connection checks, and the Chat instance.

import {
  STORAGE_KEYS, CONNECTION_CHECK_MS, MAX_MESSAGE_LENGTH, MODEL_OPTIONS, AUTO_MODEL,
} from './config.js';
import { getHealth } from './api.js';
import { Chat } from './chat.js';
import * as store from './conversations.js';
import { safeJsonParse, escapeHtml } from './utils.js';

function loadSettings() {
  return safeJsonParse(localStorage.getItem(STORAGE_KEYS.SETTINGS)) || {};
}

function saveSettings(settings) {
  localStorage.setItem(STORAGE_KEYS.SETTINGS, JSON.stringify(settings));
}

document.addEventListener('DOMContentLoaded', () => {
  const els = {
    appShell: document.getElementById('app-shell'),
    messages: document.getElementById('chat-messages'),
    welcome: document.getElementById('welcome'),
    form: document.getElementById('chat-form'),
    input: document.getElementById('chat-input'),
    sendBtn: document.getElementById('send-btn'),
    charCounter: document.getElementById('char-counter'),
    newChatBtn: document.getElementById('new-chat-btn'),
    clearBtn: document.getElementById('clear-chat-btn'),
    exportJsonBtn: document.getElementById('export-json-btn'),
    exportTxtBtn: document.getElementById('export-txt-btn'),
    connectionDot: document.getElementById('connection-dot'),
    connectionText: document.getElementById('connection-text'),
    sidebarToggle: document.getElementById('sidebar-toggle'),
    sidebarBackdrop: document.getElementById('sidebar-backdrop'),
    shortcutsToggle: document.getElementById('shortcuts-toggle'),
    shortcutsPanel: document.getElementById('shortcuts-panel'),
    convList: document.getElementById('conv-list'),
    modelSelect: document.getElementById('model-select'),
  };

  const settings = loadSettings();

  // --- Model selection ------------------------------------------------------
  if (els.modelSelect) {
    els.modelSelect.innerHTML = MODEL_OPTIONS.map(
      (opt) => `<option value="${opt.value}" title="${opt.description}">${opt.label}</option>`,
    ).join('');
    els.modelSelect.value = MODEL_OPTIONS.some((opt) => opt.value === settings.selectedModel)
      ? settings.selectedModel
      : AUTO_MODEL;
    els.modelSelect.addEventListener('change', () => {
      settings.selectedModel = els.modelSelect.value;
      saveSettings(settings);
    });
  }
  function getSelectedModel() {
    const value = els.modelSelect ? els.modelSelect.value : AUTO_MODEL;
    return value === AUTO_MODEL ? null : value;
  }

  // --- Conversation history list ------------------------------------------------------
  function renderConversationList() {
    if (!els.convList) return;
    const conversations = store.listConversations();

    if (conversations.length === 0) {
      els.convList.innerHTML = '<p class="conv-list-empty">No conversations yet.</p>';
      return;
    }

    els.convList.innerHTML = conversations.map((c) => `
      <div class="conv-item ${c.id === chat.conversationId ? 'active' : ''}" data-id="${c.id}" title="${escapeHtml(c.title)}">
        <span class="conv-item-title">${escapeHtml(c.title)}</span>
        <button type="button" class="conv-delete-btn" data-id="${c.id}" title="Delete conversation">🗑️</button>
      </div>
    `).join('');

    els.convList.querySelectorAll('.conv-item').forEach((item) => {
      item.addEventListener('click', () => {
        chat.switchTo(item.dataset.id);
        renderConversationList();
        closeSidebarIfMobile();
      });
    });
    els.convList.querySelectorAll('.conv-delete-btn').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (confirm('Delete this conversation? This cannot be undone.')) {
          chat.deleteConversation(btn.dataset.id);
        }
      });
    });
  }

  const chat = new Chat({
    messagesEl: els.messages,
    welcomeEl: els.welcome,
    getModel: getSelectedModel,
    onConversationsChanged: renderConversationList,
  });
  renderConversationList();

  // --- Message input ------------------------------------------------------
  function updateCharCounter() {
    if (!els.charCounter) return;
    const len = els.input.value.length;
    els.charCounter.textContent = `${len} / ${MAX_MESSAGE_LENGTH}`;
    els.charCounter.classList.toggle('char-counter-warn', len > MAX_MESSAGE_LENGTH * 0.9);
  }

  function autoResize() {
    els.input.style.height = 'auto';
    els.input.style.height = `${Math.min(els.input.scrollHeight, 200)}px`;
  }

  els.input.addEventListener('input', () => {
    updateCharCounter();
    autoResize();
  });

  async function handleSend() {
    const text = els.input.value;
    if (!text.trim()) return;
    els.input.value = '';
    updateCharCounter();
    autoResize();
    els.sendBtn.disabled = true;
    await chat.sendMessage(text);
    els.sendBtn.disabled = false;
    els.input.focus();
  }

  els.form.addEventListener('submit', (e) => {
    e.preventDefault();
    handleSend();
  });

  // Enter sends, Shift+Enter inserts a newline.
  els.input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  });

  updateCharCounter();

  // --- Suggestion cards ------------------------------------------------------
  document.querySelectorAll('.suggestion-card').forEach((card) => {
    card.addEventListener('click', () => {
      els.input.value = card.dataset.prompt || '';
      updateCharCounter();
      autoResize();
      els.input.focus();
    });
  });

  // --- Chat actions ---------------------------------------------------------
  if (els.newChatBtn) {
    els.newChatBtn.addEventListener('click', () => {
      chat.newConversation();
      renderConversationList();
      closeSidebarIfMobile();
      els.input.focus();
    });
  }
  if (els.clearBtn) {
    els.clearBtn.addEventListener('click', () => {
      if (!chat.conversationId) return; // nothing saved yet on this draft
      if (confirm('Delete this conversation? This cannot be undone.')) {
        chat.deleteConversation(chat.conversationId);
      }
    });
  }
  if (els.exportJsonBtn) els.exportJsonBtn.addEventListener('click', () => chat.exportChat('json'));
  if (els.exportTxtBtn) els.exportTxtBtn.addEventListener('click', () => chat.exportChat('txt'));

  // --- Sidebar open/close ---------------------------------------------------------
  // Two different mechanics share one toggle: on mobile the sidebar is an
  // off-canvas drawer (hidden by default, opened via .sidebar-open with a
  // backdrop); on desktop/tablet it's always in-flow and .sidebar-collapsed
  // hides it. Exactly one of the two classes ever has a visual effect at a
  // given viewport width, so driving both from one boolean stays in sync.
  const isDesktopViewport = () => window.matchMedia('(min-width: 768px)').matches;

  function applySidebarVisible(visible) {
    els.appShell.classList.toggle('sidebar-open', visible);
    els.appShell.classList.toggle('sidebar-collapsed', !visible);
  }

  let sidebarVisible = isDesktopViewport() ? settings.sidebarCollapsed !== true : false;
  applySidebarVisible(sidebarVisible);

  function toggleSidebar() {
    sidebarVisible = !sidebarVisible;
    applySidebarVisible(sidebarVisible);
    // Only persist as a lasting preference when toggled on desktop — the
    // mobile drawer's open/closed state is transient, per-interaction UI.
    if (isDesktopViewport()) {
      settings.sidebarCollapsed = !sidebarVisible;
      saveSettings(settings);
    }
  }

  function closeSidebarIfMobile() {
    if (isDesktopViewport()) return;
    sidebarVisible = false;
    applySidebarVisible(false);
  }

  if (els.sidebarToggle) {
    els.sidebarToggle.addEventListener('click', toggleSidebar);
  }
  if (els.sidebarBackdrop) {
    els.sidebarBackdrop.addEventListener('click', closeSidebarIfMobile);
  }

  // --- Shortcuts help ---------------------------------------------------------
  if (els.shortcutsToggle && els.shortcutsPanel) {
    els.shortcutsToggle.addEventListener('click', () => {
      els.shortcutsPanel.classList.toggle('hidden');
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === '?' && document.activeElement !== els.input) {
        els.shortcutsPanel.classList.toggle('hidden');
      }
    });
  }

  // --- Connection status ---------------------------------------------------------
  async function checkConnection() {
    try {
      const health = await getHealth();
      const onlineCount = Object.values(health.servers || {}).filter(Boolean).length;
      const total = Object.keys(health.servers || {}).length || 9;

      if (els.connectionDot) els.connectionDot.classList.add('online');
      if (els.connectionText) els.connectionText.textContent = `Connected · ${onlineCount}/${total} workers`;
    } catch {
      if (els.connectionDot) els.connectionDot.classList.remove('online');
      if (els.connectionText) els.connectionText.textContent = 'Gateway unreachable';
    }
  }

  checkConnection();
  setInterval(checkConnection, CONNECTION_CHECK_MS);

  saveSettings({ ...settings, lastVisit: Date.now() });
});
