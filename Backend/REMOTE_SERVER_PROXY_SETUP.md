# Central Proxy Setup (192.168.5.200 -> 172.16.8.170)

The AI Gateway backend now runs primarily on the **RTX 4060 Ti GPU Workstation (`172.16.8.170:8000`)** with full hardware acceleration (FLUX.1 Schnell, Faster-Whisper, and Mistral/Qwen2.5-VL).

The central server at **`192.168.5.200` (`qwen.cutm.ac.in`)** acts as the high-speed public SSL reverse proxy.

---

### Step 1: Update NGINX on `192.168.5.200`

In `/etc/nginx/sites-available/qwen` (or `/etc/nginx/conf.d/qwen.conf`), update `proxy_pass`:

Replace:
```nginx
proxy_pass http://127.0.0.1:8000;
```
With:
```nginx
proxy_pass http://172.16.8.170:8000;
```

*(Or copy the complete configuration from [`Backend/nginx_qwen.conf`](file:///c:/Users/lab74/qwen/Backend/nginx_qwen.conf))*.

---

### Step 2: Test & Reload NGINX

Run on `192.168.5.200`:
```bash
sudo nginx -t
sudo systemctl reload nginx
```

---

### Step 3: Stop Local Python Service on `192.168.5.200` (Optional but Recommended)

Since the backend is now hosted directly on `172.16.8.170:8000`, the Python process on `192.168.5.200` is no longer needed:

```bash
sudo systemctl stop qwen
sudo systemctl disable qwen
```
*(or `pkill -f qwen_lb.py` if running under nohup/tmux)*

---

### Verification

From any browser or machine:
- Chat: `https://qwen.cutm.ac.in/chat`
- Health: `https://qwen.cutm.ac.in/health`
- Direct Workstation: `http://172.16.8.170:8000/chat`
