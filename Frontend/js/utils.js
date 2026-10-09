// Shared helper functions used across the app.

import { MAX_MESSAGE_LENGTH } from './config.js';

export function validateInput(text) {
  if (typeof text !== 'string') return { valid: false, reason: 'Message must be text.' };
  const trimmed = text.trim();
  if (!trimmed) return { valid: false, reason: 'Message cannot be empty.' };
  if (trimmed.length > MAX_MESSAGE_LENGTH) {
    return { valid: false, reason: `Message is too long (max ${MAX_MESSAGE_LENGTH} characters).` };
  }
  return { valid: true, reason: null };
}

// Lightweight mirror of the gateway's FastIntentDetector so the UI can
// show an intent badge instantly, before the server responds.
export function detectIntent(prompt) {
  const text = (prompt || '').toLowerCase().trim();
  if (!text) return 'chat';

  if (text.startsWith('/image') || text.startsWith('/img') || text.startsWith('/draw') ||
      text.includes('generate image') || text.includes('generate an image') ||
      text.includes('create an image') || text.includes('draw a picture') ||
      text.includes('generate a photo') || text.includes('create a photo')) {
    return 'image_gen';
  }

  if (['def ', 'class ', 'function ', '{', '}', '```'].some((p) => text.includes(p))) {
    return 'coding';
  }

  const CODING_KEYWORDS = ['code', 'python', 'java', 'javascript', 'c++', 'cpp', 'c#', 'debug',
    'function', 'class', 'api', 'database', 'sql', 'react', 'node',
    'spring', 'fastapi', 'django', 'flask', 'git', 'docker', 'error',
    'exception', 'algorithm', 'implement', 'deploy', 'test', 'refactor'];
  const REASONING_KEYWORDS = ['explain', 'analyze', 'research', 'mathematics', 'physics', 'quantum',
    'philosophy', 'economics', 'medical', 'strategy', 'theory', 'complex'];
  const VISION_KEYWORDS = ['image', 'photo', 'screenshot', 'ocr', 'read', 'chart', 'graph',
    'diagram', 'pdf', 'document', 'visual', 'describe', 'extract'];

  const score = (words) => words.reduce((n, w) => (text.includes(w) ? n + 1 : n), 0);

  const codingScore = score(CODING_KEYWORDS);
  const reasoningScore = score(REASONING_KEYWORDS);
  const visionScore = score(VISION_KEYWORDS);

  if (visionScore > 0) return 'vision';
  if (codingScore >= reasoningScore && codingScore > 0) return 'coding';
  if (reasoningScore > codingScore && reasoningScore > 0) return 'reasoning';
  return 'chat';
}

export function formatResponse(data) {
  if (!data) return '';
  if (typeof data === 'string') return data;
  return data.response ?? '';
}

export function getTimeString(date = new Date()) {
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export function formatDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}

export function safeJsonParse(json) {
  try {
    return JSON.parse(json);
  } catch {
    return null;
  }
}

export function debounce(func, delay = 300) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => func(...args), delay);
  };
}

export function throttle(func, limit = 300) {
  let waiting = false;
  return (...args) => {
    if (waiting) return;
    func(...args);
    waiting = true;
    setTimeout(() => { waiting = false; }, limit);
  };
}

export function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str ?? '';
  return div.innerHTML;
}

// Safe Markdown-like message formatter: renders generated images, code blocks, bold, italics.
export function formatMessageHtml(text) {
  if (!text) return '';
  let str = escapeHtml(text);

  // Match Markdown image syntax: ![alt](url) - supports newlines, spaces, data URIs, and URLs
  str = str.replace(/!\[([\s\S]*?)\]\s*\(([\s\S]*?)\)/g, (match, alt, src) => {
    const cleanSrc = (src || '').trim();
    if (!cleanSrc) return match;
    return `<div class="generated-image-card" style="margin: 14px 0; border-radius: 12px; overflow: hidden; border: 1px solid rgba(255,255,255,0.18); background: #0f172a; max-width: 520px; box-shadow: 0 10px 25px -5px rgba(0,0,0,0.5);">
      <img src="${cleanSrc}" alt="${alt || 'Generated image'}" style="width: 100%; height: auto; display: block; border-radius: 12px 12px 0 0; cursor: pointer;" onclick="window.open(this.src, '_blank')" />
      <div style="padding: 10px 14px; display: flex; justify-content: space-between; align-items: center; background: rgba(15,23,42,0.9); font-size: 0.85rem; border-top: 1px solid rgba(255,255,255,0.08);">
        <span style="color: #94a3b8; font-size: 0.8rem; font-weight: 500;">🎨 Stable Diffusion v1.5</span>
        <a href="${cleanSrc}" download="generated_image.png" style="background: linear-gradient(135deg, #0284c7, #38bdf8); color: #ffffff; padding: 4px 12px; border-radius: 6px; font-weight: 600; text-decoration: none; font-size: 0.8rem; display: inline-flex; align-items: center; gap: 4px;" target="_blank">⬇ Download</a>
      </div>
    </div>`;
  });

  // Code blocks
  str = str.replace(/```([\s\S]*?)```/g, (_, code) => `<pre class="code-block"><code>${code}</code></pre>`);
  // Inline code
  str = str.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  // Bold
  str = str.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  // Italic
  str = str.replace(/\*([^*]+)\*/g, '<em>$1</em>');

  return str.replace(/\n/g, '<br>');
}

export function maskKey(key) {
  if (!key || key.length < 12) return key || '';
  return `${key.slice(0, 7)}...${key.slice(-4)}`;
}

let notificationTimer = null;
export function showNotification(message, type = 'info', duration = 3000) {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    container.className = 'toast-container';
    document.body.appendChild(container);
  }

  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  container.appendChild(toast);

  requestAnimationFrame(() => toast.classList.add('toast-visible'));

  setTimeout(() => {
    toast.classList.remove('toast-visible');
    setTimeout(() => toast.remove(), 300);
  }, duration);

  return notificationTimer;
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Fallback for browsers without Clipboard API permission
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    let ok = false;
    try {
      ok = document.execCommand('copy');
    } catch {
      ok = false;
    }
    textarea.remove();
    return ok;
  }
}

export function downloadFile(filename, content, mimeType = 'application/json') {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
