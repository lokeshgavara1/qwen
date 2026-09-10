// Multi-conversation history store (localStorage-backed), the same shape
// ChatGPT/Claude expose: a list of past chats in the sidebar, each with its
// own title and message log, switchable and deletable independently.

import {
  STORAGE_KEYS, MAX_CONVERSATIONS, MAX_MESSAGES_PER_CONVERSATION, CONVERSATION_TITLE_MAX_LEN,
} from './config.js';
import { safeJsonParse } from './utils.js';

function readAll() {
  const parsed = safeJsonParse(localStorage.getItem(STORAGE_KEYS.CONVERSATIONS));
  return Array.isArray(parsed) ? parsed : [];
}

function writeAll(list) {
  localStorage.setItem(STORAGE_KEYS.CONVERSATIONS, JSON.stringify(list));
}

// One-time migration from the old single-thread history format, so nobody's
// existing chat silently disappears when this ships.
export function migrateLegacyHistory() {
  const legacyRaw = localStorage.getItem(STORAGE_KEYS.LEGACY_CHAT_HISTORY);
  if (!legacyRaw) return;

  const legacyMessages = safeJsonParse(legacyRaw);
  localStorage.removeItem(STORAGE_KEYS.LEGACY_CHAT_HISTORY);

  if (!Array.isArray(legacyMessages) || legacyMessages.length === 0) return;
  if (readAll().length > 0) return; // don't clobber a store that already has conversations

  const firstUserMsg = legacyMessages.find((m) => m.type === 'user');
  const now = Date.now();
  const migrated = {
    id: crypto.randomUUID(),
    title: makeTitle(firstUserMsg ? firstUserMsg.text : 'Previous chat'),
    createdAt: legacyMessages[0]?.timestamp || now,
    updatedAt: legacyMessages[legacyMessages.length - 1]?.timestamp || now,
    messages: legacyMessages,
  };
  writeAll([migrated]);
  localStorage.setItem(STORAGE_KEYS.ACTIVE_CONVERSATION, migrated.id);
}

export function makeTitle(text) {
  const clean = (text || '').replace(/\s+/g, ' ').trim();
  if (!clean) return 'New chat';
  return clean.length > CONVERSATION_TITLE_MAX_LEN
    ? `${clean.slice(0, CONVERSATION_TITLE_MAX_LEN).trim()}…`
    : clean;
}

/** Conversations sorted most-recently-updated first, for sidebar display. */
export function listConversations() {
  return readAll().sort((a, b) => b.updatedAt - a.updatedAt);
}

export function getConversation(id) {
  return readAll().find((c) => c.id === id) || null;
}

export function getActiveId() {
  return localStorage.getItem(STORAGE_KEYS.ACTIVE_CONVERSATION) || null;
}

export function setActiveId(id) {
  if (id) {
    localStorage.setItem(STORAGE_KEYS.ACTIVE_CONVERSATION, id);
  } else {
    localStorage.removeItem(STORAGE_KEYS.ACTIVE_CONVERSATION);
  }
}

/**
 * Persist a conversation (insert or update). Trims message count, caps the
 * total number of stored conversations by dropping the oldest.
 */
export function saveConversation(conversation) {
  const list = readAll();
  const idx = list.findIndex((c) => c.id === conversation.id);
  const trimmedMessages = conversation.messages.slice(-MAX_MESSAGES_PER_CONVERSATION);
  const toSave = { ...conversation, messages: trimmedMessages, updatedAt: Date.now() };

  if (idx >= 0) {
    list[idx] = toSave;
  } else {
    list.push(toSave);
  }

  list.sort((a, b) => b.updatedAt - a.updatedAt);
  writeAll(list.slice(0, MAX_CONVERSATIONS));
  return toSave;
}

/** Returns the id of the conversation that should become active next, or null. */
export function deleteConversation(id) {
  const list = readAll().filter((c) => c.id !== id);
  writeAll(list);
  if (getActiveId() === id) {
    const next = list.sort((a, b) => b.updatedAt - a.updatedAt)[0];
    setActiveId(next ? next.id : null);
    return next ? next.id : null;
  }
  return getActiveId();
}
