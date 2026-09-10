// Chat state, rendering, and message lifecycle — operates on one
// conversation at a time, backed by the multi-conversation store.

import { generateResponse, GatewayError } from './api.js';
import * as store from './conversations.js';
import {
  validateInput, detectIntent, formatMessageHtml, getTimeString,
  showNotification, copyText, downloadFile,
} from './utils.js';

export class Chat {
  constructor({ messagesEl, welcomeEl, getModel, onConversationsChanged }) {
    this.messagesEl = messagesEl;
    this.welcomeEl = welcomeEl || null;
    this.getModel = getModel || (() => null);
    this.onConversationsChanged = onConversationsChanged || (() => {});
    this.loadingEl = null;

    store.migrateLegacyHistory();

    const activeId = store.getActiveId();
    const active = activeId ? store.getConversation(activeId) : null;
    if (active) {
      this.conversationId = active.id;
      this.messages = active.messages;
    } else {
      this.startDraft();
    }
    this.renderAll();
  }

  /** An unsaved, empty conversation — not written to the store until the first message is sent. */
  startDraft() {
    this.conversationId = null;
    this.messages = [];
    store.setActiveId(null);
  }

  newConversation() {
    if (!this.conversationId && this.messages.length === 0) return; // already on a fresh draft
    this.startDraft();
    this.renderAll();
  }

  switchTo(id) {
    if (id === this.conversationId) return;
    const conv = store.getConversation(id);
    if (!conv) return;
    this.conversationId = conv.id;
    this.messages = conv.messages;
    store.setActiveId(conv.id);
    this.renderAll();
  }

  deleteConversation(id) {
    const nextId = store.deleteConversation(id);
    if (id === this.conversationId) {
      const next = nextId ? store.getConversation(nextId) : null;
      if (next) {
        this.conversationId = next.id;
        this.messages = next.messages;
      } else {
        this.startDraft();
      }
      this.renderAll();
    }
    this.onConversationsChanged();
  }

  persist() {
    const firstUserMsg = this.messages.find((m) => m.type === 'user');
    const conversation = {
      id: this.conversationId || crypto.randomUUID(),
      title: store.makeTitle(firstUserMsg ? firstUserMsg.text : ''),
      createdAt: this.messages[0]?.timestamp || Date.now(),
      messages: this.messages,
    };
    const saved = store.saveConversation(conversation);
    this.conversationId = saved.id;
    store.setActiveId(saved.id);
    this.onConversationsChanged();
  }

  renderAll() {
    this.messagesEl.innerHTML = '';
    this.updateWelcomeVisibility();
    this.messages.forEach((msg) => this.renderMessage(msg));
    this.scrollToBottom();
  }

  updateWelcomeVisibility() {
    if (!this.welcomeEl) return;
    this.welcomeEl.classList.toggle('hidden', this.messages.length > 0);
  }

  renderMessage(msg) {
    this.updateWelcomeVisibility();

    const wrapper = document.createElement('div');
    wrapper.className = `msg msg-${msg.type} fade-in`;
    wrapper.dataset.id = msg.id;

    if (msg.type === 'ai') {
      const avatar = document.createElement('div');
      avatar.className = 'msg-avatar';
      avatar.textContent = '⚡';
      wrapper.appendChild(avatar);
    }

    const body = document.createElement('div');
    body.className = 'msg-body';

    const textEl = document.createElement('div');
    textEl.className = msg.type === 'user' ? 'msg-bubble' : 'msg-text';
    textEl.innerHTML = formatMessageHtml(msg.text);
    body.appendChild(textEl);

    const actions = document.createElement('div');
    actions.className = 'msg-actions';

    const copyBtn = document.createElement('button');
    copyBtn.className = 'msg-action-btn';
    copyBtn.type = 'button';
    copyBtn.title = 'Copy message';
    copyBtn.textContent = '📋';
    copyBtn.addEventListener('click', async () => {
      const ok = await copyText(msg.text);
      showNotification(ok ? 'Copied!' : 'Copy failed', ok ? 'success' : 'error', 1500);
    });
    actions.appendChild(copyBtn);

    const meta = document.createElement('span');
    meta.className = 'msg-meta';
    const metaParts = [getTimeString(new Date(msg.timestamp))];
    if (msg.type === 'ai' && msg.intent) metaParts.push(msg.intent);
    if (msg.type === 'ai' && msg.model) metaParts.push(msg.model);
    if (msg.type === 'ai' && msg.cached) metaParts.push('cached');
    if (msg.type === 'ai' && msg.elapsedMs) metaParts.push(`${msg.elapsedMs}ms`);
    meta.textContent = metaParts.join(' · ');
    actions.appendChild(meta);

    body.appendChild(actions);
    wrapper.appendChild(body);
    this.messagesEl.appendChild(wrapper);
    return wrapper;
  }

  scrollToBottom() {
    this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
  }

  addLoadingMessage() {
    this.updateWelcomeVisibility();
    const wrapper = document.createElement('div');
    wrapper.className = 'msg msg-ai msg-loading fade-in';
    wrapper.innerHTML = `
      <div class="msg-avatar">⚡</div>
      <div class="msg-body">
        <div class="typing-indicator"><span></span><span></span><span></span></div>
      </div>
    `;
    this.messagesEl.appendChild(wrapper);
    this.loadingEl = wrapper;
    this.scrollToBottom();
    return wrapper;
  }

  removeLoadingMessage() {
    if (this.loadingEl) {
      this.loadingEl.remove();
      this.loadingEl = null;
    }
  }

  exportChat(format = 'json') {
    if (this.messages.length === 0) {
      showNotification('Nothing to export yet.', 'info');
      return;
    }
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    if (format === 'txt') {
      const text = this.messages
        .map((m) => `[${getTimeString(new Date(m.timestamp))}] ${m.type === 'user' ? 'You' : 'AI'}: ${m.text}`)
        .join('\n\n');
      downloadFile(`chat-export-${stamp}.txt`, text, 'text/plain');
    } else {
      downloadFile(`chat-export-${stamp}.json`, JSON.stringify(this.messages, null, 2), 'application/json');
    }
    showNotification('Chat exported.', 'success');
  }

  async sendMessage(rawText) {
    const { valid, reason } = validateInput(rawText);
    if (!valid) {
      showNotification(reason, 'error');
      return;
    }
    const text = rawText.trim();
    const intentGuess = detectIntent(text);

    const userMsg = {
      id: crypto.randomUUID(),
      type: 'user',
      text,
      timestamp: Date.now(),
      intent: intentGuess,
    };
    this.messages.push(userMsg);
    this.renderMessage(userMsg);
    this.scrollToBottom();
    this.persist();

    this.addLoadingMessage();
    const startedAt = performance.now();

    let aiText = '';
    let liveBubble = null;

    try {
      const result = await generateResponse(text, {
        stream: true,
        model: this.getModel(),
        onToken: (_chunk, full) => {
          aiText = full;
          if (!liveBubble) {
            this.removeLoadingMessage();
            liveBubble = this.addStreamingBubble();
          }
          liveBubble.querySelector('.msg-text').innerHTML = formatMessageHtml(aiText);
          this.scrollToBottom();
        },
      });

      this.removeLoadingMessage();
      if (liveBubble) liveBubble.remove();

      const elapsedMs = Math.round(performance.now() - startedAt);
      const aiMsg = {
        id: crypto.randomUUID(),
        type: 'ai',
        text: result.text || '(empty response)',
        timestamp: Date.now(),
        intent: intentGuess,
        model: result.model,
        server: result.server,
        cached: result.cached,
        elapsedMs,
      };
      this.messages.push(aiMsg);
      this.renderMessage(aiMsg);
      this.scrollToBottom();
      this.persist();
    } catch (err) {
      this.removeLoadingMessage();
      if (liveBubble) liveBubble.remove();
      const message = err instanceof GatewayError ? err.message : 'Unexpected error contacting the gateway.';
      const errMsg = {
        id: crypto.randomUUID(),
        type: 'ai',
        text: `⚠️ ${message}`,
        timestamp: Date.now(),
        isError: true,
      };
      this.messages.push(errMsg);
      this.renderMessage(errMsg);
      this.scrollToBottom();
      this.persist();
      showNotification(message, 'error');
    }
  }

  addStreamingBubble() {
    const wrapper = document.createElement('div');
    wrapper.className = 'msg msg-ai fade-in';
    wrapper.innerHTML = `
      <div class="msg-avatar">⚡</div>
      <div class="msg-body"><div class="msg-text"></div></div>
    `;
    this.messagesEl.appendChild(wrapper);
    return wrapper;
  }
}
