import http.server
import json
import os
import time
import urllib.request
import urllib.parse
import threading
import secrets
import base64
import ssl

PORT = int(os.environ.get("PORT", 8080))
CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("REDIRECT_URI", "http://localhost:8080/auth/callback")
SHM_DIR = "/dev/shm/users"
os.makedirs(SHM_DIR, exist_ok=True)

USER_SESSIONS = {}

def get_k8s_context():
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    if not os.path.exists(token_path):
        return None, None
    with open(token_path) as f:
        k8s_token = f.read().strip()
    ctx = ssl.create_default_context(cafile=ca_path)
    return k8s_token, ctx

def save_user_session(username, data):
    user_file = os.path.join(SHM_DIR, f"{username}.json")
    tmp_file = f"{user_file}.tmp"
    with open(tmp_file, "w") as f:
        json.dump(data, f)
    os.replace(tmp_file, user_file)

    k8s_token, ctx = get_k8s_context()
    if not k8s_token:
        return
    sec_name = f"copilot-user-{username.lower()}"
    raw_payload = json.dumps(data).encode("utf-8")
    b64_payload = base64.b64encode(raw_payload).decode("utf-8")
    secret_manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": sec_name, "namespace": "kagent"},
        "type": "Opaque",
        "data": {"session": b64_payload}
    }
    url = "https://kubernetes.default.svc/api/v1/namespaces/kagent/secrets"
    req = urllib.request.Request(url, data=json.dumps(secret_manifest).encode(),
                                 headers={"Authorization": f"Bearer {k8s_token}", "Content-Type": "application/json"},
                                 method="POST")
    try:
        urllib.request.urlopen(req, context=ctx)
    except urllib.error.HTTPError as e:
        if e.code == 409:
            put_req = urllib.request.Request(f"{url}/{sec_name}", data=json.dumps(secret_manifest).encode(),
                                             headers={"Authorization": f"Bearer {k8s_token}", "Content-Type": "application/json"},
                                             method="PUT")
            urllib.request.urlopen(put_req, context=ctx)

def fetch_copilot_token(oauth_token):
    req = urllib.request.Request(
        "https://api.github.com/copilot_internal/v2/token",
        headers={"Authorization": f"token {oauth_token}", "User-Agent": "GitHubCopilotChat/0.22.0"}
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())

def background_rotator():
    while True:
        try:
            for user_file in os.listdir(SHM_DIR):
                if not user_file.endswith(".json"): continue
                filepath = os.path.join(SHM_DIR, user_file)
                with open(filepath) as f:
                    data = json.load(f)
                
                if time.time() >= (data.get("expires_at", 0) - 300):
                    new_token = fetch_copilot_token(data["oauth_token"])
                    data["copilot_token"] = new_token["token"]
                    data["expires_at"] = new_token["expires_at"]
                    save_user_session(data["username"], data)
                    USER_SESSIONS[data["api_key"]] = data
                    print(f"[Rotator] {data['username']} token yenilendi.", flush=True)
        except Exception as e:
            print(f"[Rotator Error] {e}", flush=True)
        time.sleep(60)

threading.Thread(target=background_rotator, daemon=True).start()

LOGIN_HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Kagent • Copilot AI Gateway</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-base: #0b0f17;
            --bg-surface: #111827;
            --border: #1f2937;
            --text-primary: #f9fafb;
            --text-secondary: #9ca3af;
            --accent: #2563eb;
            --success: #10b981;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Plus Jakarta Sans', sans-serif;
            background-color: var(--bg-base);
            background-image: 
                radial-gradient(at 0% 0%, rgba(37, 99, 235, 0.12) 0px, transparent 50%),
                radial-gradient(at 100% 100%, rgba(16, 185, 129, 0.08) 0px, transparent 50%);
            color: var(--text-primary);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 24px;
        }
        .container {
            width: 100%;
            max-width: 440px;
            background: var(--bg-surface);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 36px 32px;
            box-shadow: 0 20px 40px -15px rgba(0, 0, 0, 0.5);
            text-align: center;
        }
        .brand-badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 6px 12px;
            background: rgba(37, 99, 235, 0.1);
            border: 1px solid rgba(37, 99, 235, 0.25);
            border-radius: 9999px;
            font-size: 12px;
            font-weight: 600;
            color: #60a5fa;
            margin-bottom: 20px;
        }
        .brand-dot {
            width: 6px;
            height: 6px;
            background: #3b82f6;
            border-radius: 50%;
            box-shadow: 0 0 8px #3b82f6;
        }
        h1 {
            font-size: 22px;
            font-weight: 700;
            letter-spacing: -0.02em;
            margin-bottom: 8px;
        }
        p.subtitle {
            font-size: 14px;
            color: var(--text-secondary);
            line-height: 1.5;
            margin-bottom: 28px;
        }
        .btn-github {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 12px;
            width: 100%;
            padding: 13px 20px;
            background: #f9fafb;
            color: #0b0f17;
            text-decoration: none;
            font-weight: 600;
            font-size: 14px;
            border-radius: 10px;
            transition: all 0.2s ease;
        }
        .btn-github:hover {
            background: #e5e7eb;
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(255, 255, 255, 0.15);
        }
        .btn-github svg { width: 20px; height: 20px; }
        .footer-note {
            margin-top: 24px;
            padding-top: 20px;
            border-top: 1px solid var(--border);
            font-size: 12px;
            color: var(--text-secondary);
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
        }
        .lock-icon { width: 14px; height: 14px; color: var(--success); }
    </style>
</head>
<body>
    <div class="container">
        <div class="brand-badge">
            <span class="brand-dot"></span>
            Kagent • Multi-Tenant Gateway
        </div>
        <h1>Kurumsal AI Erişimi</h1>
        <p class="subtitle">Kubernetes cluster ajanlarınız için GitHub Copilot oturumu başlatın ve izole API anahtarınızı alın.</p>
        
        <a class="btn-github" href="https://github.com/login/oauth/authorize?client_id=CLIENT_ID_PLACEHOLDER&scope=read:user&redirect_uri=REDIRECT_URI_PLACEHOLDER">
            <svg viewBox="0 0 24 24" fill="currentColor">
                <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z"/>
            </svg>
            GitHub ile Yetkilendir
        </a>

        <div class="footer-note">
            <svg class="lock-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>
                <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>
            </svg>
            Sıfır Disk Sızıntısı • Bellek İçi Oturum Rotasyonu
        </div>
    </div>
</body>
</html>
"""

CALLBACK_SUCCESS_HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Yetkilendirme Başarılı • Kagent Gateway</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-base: #0b0f17;
            --bg-surface: #111827;
            --border: #1f2937;
            --text-primary: #f9fafb;
            --text-secondary: #9ca3af;
            --accent: #3b82f6;
            --success: #10b981;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Plus Jakarta Sans', sans-serif;
            background-color: var(--bg-base);
            color: var(--text-primary);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 24px;
        }
        .card {
            width: 100%;
            max-width: 580px;
            background: var(--bg-surface);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 32px;
            box-shadow: 0 20px 40px -15px rgba(0, 0, 0, 0.5);
        }
        .header {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 24px;
        }
        .avatar {
            width: 44px;
            height: 44px;
            border-radius: 50%;
            border: 2px solid var(--success);
        }
        .user-meta h2 {
            font-size: 18px;
            font-weight: 700;
        }
        .user-meta span {
            font-size: 13px;
            color: var(--success);
            font-weight: 500;
        }
        .section-title {
            font-size: 13px;
            font-weight: 600;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 8px;
        }
        .key-container {
            display: flex;
            align-items: center;
            background: #06090e;
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 10px 14px;
            margin-bottom: 24px;
        }
        .key-text {
            font-family: 'JetBrains Mono', monospace;
            font-size: 13px;
            color: #60a5fa;
            flex-grow: 1;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .copy-btn {
            background: #1f2937;
            border: none;
            color: var(--text-primary);
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 12px;
            cursor: pointer;
            transition: all 0.2s;
        }
        .copy-btn:hover { background: #374151; }
        pre {
            background: #06090e;
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 14px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 12px;
            color: #d1d5db;
            overflow-x: auto;
            line-height: 1.5;
            margin-bottom: 20px;
        }
        .badge {
            display: inline-block;
            background: rgba(16, 185, 129, 0.1);
            color: var(--success);
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
        }
    </style>
</head>
<body>
    <div class="card">
        <div class="header">
            <img class="avatar" src="USER_AVATAR" alt="Avatar">
            <div class="user-meta">
                <h2>Hoş Geldiniz, @USER_NAME</h2>
                <span>✓ Copilot Oturumu Doğrulandı & RAM'e Yazıldı</span>
            </div>
        </div>

        <div class="section-title">Kişisel Kagent API Anahtarınız</div>
        <div class="key-container">
            <span class="key-text" id="apiKey">API_KEY_PLACEHOLDER</span>
            <button class="copy-btn" onclick="copyKey()">Kopyala</button>
        </div>

        <div class="section-title">Kagent ModelConfig Manifestiniz</div>
        <pre><code>apiVersion: v1
kind: Secret
metadata:
  name: kagent-user-USER_NAME_LOWER
  namespace: kagent
type: Opaque
stringData:
  api-key: "API_KEY_PLACEHOLDER"
---
apiVersion: kagent.dev/v1alpha2
kind: ModelConfig
metadata:
  name: copilot-USER_NAME_LOWER
  namespace: kagent
spec:
  provider: OpenAI
  model: "gpt-4o"
  apiKeySecret: kagent-user-USER_NAME_LOWER
  apiKeySecretKey: api-key
  openAI:
    baseUrl: "http://copilot-gateway-svc.kagent.svc.cluster.local:8080/v1"</code></pre>

        <span class="badge">Autonomously Rotated every 15 min</span>
    </div>

    <script>
        function copyKey() {
            const key = document.getElementById('apiKey').innerText;
            navigator.clipboard.writeText(key);
            const btn = document.querySelector('.copy-btn');
            btn.innerText = 'Kopyalandı!';
            setTimeout(() => { btn.innerText = 'Kopyala'; }, 2000);
        }
    </script>
</body>
</html>
"""

class MultiTenantHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        
        if url.path == "/" or url.path == "/login":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = LOGIN_HTML.replace("CLIENT_ID_PLACEHOLDER", CLIENT_ID).replace("REDIRECT_URI_PLACEHOLDER", REDIRECT_URI)
            self.wfile.write(html.encode())

        elif url.path == "/auth/callback":
            try:
                qs = urllib.parse.parse_qs(url.query)
                code = qs.get("code", [None])[0]
                if not code:
                    self.send_response(400)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"Authorization code eksik.")
                    return

                # 1. GitHub OAuth Access Token takası
                token_url = "https://github.com/login/oauth/access_token"
                token_params = urllib.parse.urlencode({
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "code": code,
                    "redirect_uri": REDIRECT_URI
                }).encode("utf-8")
                
                req = urllib.request.Request(
                    token_url,
                    data=token_params,
                    headers={"Accept": "application/json", "User-Agent": "KagentGateway/1.0"}
                )
                with urllib.request.urlopen(req) as resp:
                    resp_body = resp.read().decode("utf-8")
                    try:
                        token_data = json.loads(resp_body)
                    except Exception:
                        token_data = dict(urllib.parse.parse_qsl(resp_body))

                oauth_token = token_data.get("access_token")
                if not oauth_token:
                    error_msg = token_data.get("error_description", token_data.get("error", "OAuth token alinamadi"))
                    self.send_response(400)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(f"GitHub OAuth Hatasi: {error_msg}".encode())
                    return

                # 2. Kullanıcı bilgilerini al
                user_req = urllib.request.Request(
                    "https://api.github.com/user",
                    headers={"Authorization": f"token {oauth_token}", "User-Agent": "KagentGateway/1.0"}
                )
                with urllib.request.urlopen(user_req) as resp:
                    user_info = json.loads(resp.read().decode("utf-8"))
                username = user_info.get("login", "unknown")
                avatar_url = user_info.get("avatar_url", "https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png")

                # 3. Copilot Internal Token al
                copilot_data = fetch_copilot_token(oauth_token)
                api_key = f"sk-kagent-{secrets.token_hex(16)}"

                session_data = {
                    "username": username,
                    "api_key": api_key,
                    "oauth_token": oauth_token,
                    "copilot_token": copilot_data["token"],
                    "expires_at": copilot_data["expires_at"]
                }
                save_user_session(username, session_data)
                USER_SESSIONS[api_key] = session_data

                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                
                res_html = (CALLBACK_SUCCESS_HTML
                            .replace("USER_NAME_LOWER", username.lower())
                            .replace("USER_NAME", username)
                            .replace("USER_AVATAR", avatar_url)
                            .replace("API_KEY_PLACEHOLDER", api_key))
                self.wfile.write(res_html.encode("utf-8"))

            except urllib.error.HTTPError as e:
                err_content = e.read().decode("utf-8", errors="replace")
                print(f"[Callback HTTPError {e.code}] {err_content}", flush=True)
                self.send_response(e.code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(f"<h3>GitHub API Hatasi ({e.code})</h3><pre>{err_content}</pre>".encode())
            except Exception as e:
                print(f"[Callback Error] {e}", flush=True)
                self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(f"Sunucu Hatasi: {e}".encode())

        elif url.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path.startswith("/v1/chat/completions"):
            auth_header = self.headers.get("Authorization", "")
            token = auth_header.replace("Bearer ", "").strip()
            
            session = USER_SESSIONS.get(token)
            if not session:
                for f in os.listdir(SHM_DIR):
                    if f.endswith(".json"):
                        with open(os.path.join(SHM_DIR, f)) as sfile:
                            c = json.load(sfile)
                            if c.get("api_key") == token:
                                session = c
                                USER_SESSIONS[token] = session
                                break
            
            if not session:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Gecersiz Kagent API Key"}).encode())
                return

            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)

            upstream_url = "https://api.githubcopilot.com/chat/completions"
            req = urllib.request.Request(
                upstream_url,
                data=body,
                headers={
                    "Authorization": f"Bearer {session['copilot_token']}",
                    "Content-Type": "application/json",
                    "User-Agent": "GitHubCopilotChat/0.22.0",
                    "Openai-Intent": "conversation-panel"
                },
                method="POST"
            )

            try:
                with urllib.request.urlopen(req) as resp:
                    self.send_response(resp.status)
                    for k, v in resp.headers.items():
                        self.send_header(k, v)
                    self.end_headers()
                    while True:
                        chunk = resp.read(1024)
                        if not chunk: break
                        self.wfile.write(chunk)
            except urllib.error.HTTPError as e:
                self.send_response(e.code)
                self.end_headers()
                self.wfile.write(e.read())

if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), MultiTenantHandler)
    print(f"Multi-Tenant Gateway aktif: http://0.0.0.0:{PORT}", flush=True)
    server.serve_forever()
