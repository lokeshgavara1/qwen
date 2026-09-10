// Central configuration for the CUTM AI Gateway Chat Frontend.
// Matches the live gateway implementation (qwen_lb.py).

export const APP_NAME = 'CUTM AI Gateway Chat Frontend';

// Auto-detect gateway URL with support for localhost, custom override, and university network
export const GATEWAY_URL = (() => {
  const custom = typeof localStorage !== 'undefined' ? localStorage.getItem('aig_gateway_url') : null;
  if (custom) return custom;

  if (typeof window !== 'undefined') {
    // If running directly on the gateway port 8000, use same origin
    if (window.location.port === '8000') {
      return window.location.origin;
    }
    // If running on local dev server (e.g., :3000, :5500)
    if (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1') {
      return 'http://localhost:8000';
    }
  }
  // Default to gateway internal IP over HTTP
  return 'http://172.16.8.4:8000';
})();

export const TARGET_HOST = 'qwen.cutm.ac.in';

export const AUTO_MODEL = 'auto';
export const MODEL_OPTIONS = [
  { value: AUTO_MODEL, label: 'Auto', description: 'Picks the right model per message' },
  { value: 'mistral:7b', label: 'Mistral 7B', description: 'Chat, coding, reasoning' },
  { value: 'qwen2.5vl:7b', label: 'Qwen2.5-VL 7B', description: 'Vision' },
];

export const INTENTS = {
  CHAT: 'chat',
  CODING: 'coding',
  VISION: 'vision',
  REASONING: 'reasoning',
};

export const MAX_TOKENS = {
  chat: 80,
  coding: 150,
  vision: 60,
  reasoning: 100,
};

export const CONNECTION_CHECK_MS = 15000;
export const MAX_MESSAGE_LENGTH = 4000;
export const MAX_MESSAGES_PER_CONVERSATION = 200;
export const MAX_CONVERSATIONS = 60;
export const CONVERSATION_TITLE_MAX_LEN = 48;

export const STORAGE_KEYS = {
  CONVERSATIONS: 'aig_conversations',
  ACTIVE_CONVERSATION: 'aig_active_conversation',
  SETTINGS: 'aig_settings',
  LEGACY_CHAT_HISTORY: 'aig_chat_history',
};

export const ENDPOINTS = {
  GENERATE: '/api/generate',
  HEALTH: '/health',
};
