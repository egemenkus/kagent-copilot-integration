import http.server
import json
import os
import time
import urllib.parse
import threading
import secrets
import base64
import requests

PORT = int(os.environ.get("PORT", 8080))
SHM_DIR = "/dev/shm/users"
os.makedirs(SHM_DIR, exist_ok=True)

USER_SESSIONS = {}

COPILOT_CLIENT_ID = "01ab8ac9400c4e429b23"

COPILOT_HEADERS = {
    "User-Agent": "GitHubCopilotChat/0.22.0",
    "Editor-Version": "vscode/1.93.0",
    "Editor-Plugin-Version": "copilot-chat/0.22.0",
    "Accept": "application/json"
}

def get_k8s_context():
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    if not os.path.exists(token_path):
        return None, None
    with open(token_path) as f:
        k8s_token = f.read().strip()
    return k8s_token, ca_path

def apply_k8s_resource(url, manifest, ca_path, k8s_token):
    headers = {"Authorization": f"Bearer {k8s_token}", "Content-Type": "application/json"}
    resp = requests.post(url, json=manifest, headers=headers, verify=ca_path, timeout=5)
    if resp.status_code == 409:
        name = manifest["metadata"]["name"]
        requests.put(f"{url}/{name}", json=manifest, headers=headers, verify=ca_path, timeout=5)

def provision_user_k8s_resources(username, api_key, session_data):
    k8s_token, ca_path = get_k8s_context()
    if not k8s_token:
        print("[Provision] In-cluster K8s ServiceAccount bulunamadi.", flush=True)
        return

    uname_lower = username.lower()

    # 1. Dahili Oturum Secret'ı (Gateway Pod'u restart olursa kurtarmak için)
    sec_name = f"copilot-user-{uname_lower}"
    raw_payload = json.dumps(session_data).encode("utf-8")
    b64_payload = base64.b64encode(raw_payload).decode("utf-8")
    session_manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": sec_name, "namespace": "kagent"},
        "type": "Opaque",
        "data": {"session": b64_payload}
    }
    apply_k8s_resource("https://kubernetes.default.svc/api/v1/namespaces/kagent/secrets", session_manifest, ca_path, k8s_token)

    # 2. Kagent API Key Secret'ı (ModelConfig'in okuyacağı secret)
    kagent_sec_name = f"kagent-user-{uname_lower}"
    b64_key = base64.b64encode(api_key.encode("utf-8")).decode("utf-8")
    key_manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": kagent_sec_name, "namespace": "kagent"},
        "type": "Opaque",
        "data": {"api-key": b64_key}
    }
    apply_k8s_resource("https://kubernetes.default.svc/api/v1/namespaces/kagent/secrets", key_manifest, ca_path, k8s_token)

    # 3. Kagent ModelConfig CRD (Otomatik Provisioning!)
    modelconfig_name = f"copilot-{uname_lower}"
    modelconfig_manifest = {
        "apiVersion": "kagent.dev/v1alpha2",
        "kind": "ModelConfig",
        "metadata": {
            "name": modelconfig_name,
            "namespace": "kagent"
        },
        "spec": {
            "provider": "OpenAI",
            "model": "gpt-4o",
            "apiKeySecret": kagent_sec_name,
            "apiKeySecretKey": "api-key",
            "openAI": {
                "baseUrl": "http://copilot-gateway-svc.kagent.svc.cluster.local:8080/v1"
            }
        }
    }
    apply_k8s_resource("https://kubernetes.default.svc/apis/kagent.dev/v1alpha2/namespaces/kagent/modelconfigs", modelconfig_manifest, ca_path, k8s_token)
    print(f"[Provision] {username} icin ModelConfig ve Secret basariyla otomatik olusturuldu.", flush=True)

def save_user_session(username, data):
    user_file = os.path.join(SHM_DIR, f"{username}.json")
    tmp_file = f"{user_file}.tmp"
    with open(tmp_file, "w") as f:
        json.dump(data, f)
    os.replace(tmp_file, user_file)

    provision_user_k8s_resources(username, data["api_key"], data)

def fetch_copilot_token(github_token):
    headers = dict(COPILOT_HEADERS)
    headers["Authorization"] = f"token {github_token}"
    resp = requests.get("https://api.github.com/copilot_internal/v2/token", headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()

def background_rotator():
    while True:
        try:
            for user_file in os.listdir(SHM_DIR):
                if not user_file.endswith(".json"): continue
                filepath = os.path.join(SHM_DIR, user_file)
                with open(filepath) as f:
                    data = json.load(f)
                
                if time.time() >= (data.get("expires_at", 0) - 300):
                    new_token = fetch_copilot_token(data["github_token"])
                    data["copilot_token"] = new_token["token"]
                    data["expires_at"] = new_token["expires_at"]
                    save_user_session(data["username"], data)
                    USER_SESSIONS[data["api_key"]] = data
                    print(f"[Rotator] {data['username']} token yenilendi.", flush=True)
        except Exception as e:
            print(f"[Rotator Error] {e}", flush=True)
        time.sleep(60)

threading.Thread(target=background_rotator, daemon=True).start()
def load_sessions_from_k8s():
    k8s_token, ca_path = get_k8s_context()
    if not k8s_token:
        return
    url = "https://kubernetes.default.svc/api/v1/namespaces/kagent/secrets"
    headers = {"Authorization": f"Bearer {k8s_token}"}
    try:
        resp = requests.get(url, headers=headers, verify=ca_path, timeout=5)
        if resp.status_code == 200:
            for item in resp.json().get("items", []):
                name = item["metadata"]["name"]
                if name.startswith("copilot-user-"):
                    b64_data = item.get("data", {}).get("session")
                    if b64_data:
                        raw = base64.b64decode(b64_data).decode("utf-8")
                        sess = json.loads(raw)
                        uname = sess["username"]
                        save_file = os.path.join(SHM_DIR, f"{uname}.json")
                        with open(save_file, "w") as sf:
                            json.dump(sess, sf)
                        USER_SESSIONS[sess["api_key"]] = sess
                        print(f"[Init] {uname} oturumu Secret'tan RAM'e yuklendi.", flush=True)
    except Exception as e:
        print(f"[Init Warning] Oturumlar yuklenemedi: {e}", flush=True)

load_sessions_from_k8s()


INDEX_HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Kagent • Copilot AI Gateway</title>
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
            background: var(--bg-base);
            background-image: radial-gradient(at 0% 0%, rgba(37, 99, 235, 0.12) 0px, transparent 50%),
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
            max-width: 520px;
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
        .brand-dot { width: 6px; height: 6px; background: #3b82f6; border-radius: 50%; box-shadow: 0 0 8px #3b82f6; }
        h1 { font-size: 22px; font-weight: 700; margin-bottom: 8px; }
        p.subtitle { font-size: 14px; color: var(--text-secondary); line-height: 1.5; margin-bottom: 28px; }
        .btn {
            display: inline-flex;
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
            border: none;
            cursor: pointer;
            transition: all 0.2s ease;
        }
        .btn:hover { background: #e5e7eb; transform: translateY(-1px); }
        .btn svg { width: 20px; height: 20px; }
        .code-box {
            display: none;
            margin-top: 24px;
            text-align: left;
            background: #06090e;
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 20px;
        }
        .user-code {
            font-family: 'JetBrains Mono', monospace;
            font-size: 28px;
            font-weight: 700;
            letter-spacing: 4px;
            color: #60a5fa;
            text-align: center;
            margin: 14px 0;
            padding: 10px;
            background: #0d131f;
            border: 1px dashed #2563eb;
            border-radius: 8px;
            cursor: pointer;
        }
        .spinner {
            display: inline-block;
            width: 14px;
            height: 14px;
            border: 2px solid rgba(255,255,255,0.3);
            border-radius: 50%;
            border-top-color: #fff;
            animation: spin 1s ease-in-out infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        .success-box { display: none; text-align: left; margin-top: 24px; }
        .status-badge {
            background: rgba(16, 185, 129, 0.1);
            border: 1px solid rgba(16, 185, 129, 0.3);
            color: #34d399;
            padding: 12px 16px;
            border-radius: 8px;
            font-size: 13px;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .detail-item {
            background: #06090e;
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 12px 14px;
            margin-bottom: 12px;
        }
        .detail-title {
            font-size: 11px;
            font-weight: 600;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 4px;
        }
        .detail-value {
            font-family: 'JetBrains Mono', monospace;
            font-size: 13px;
            color: #60a5fa;
            word-break: break-all;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="brand-badge">
            <span class="brand-dot"></span>
            Kagent • Copilot Auth Portal
        </div>

        <div id="initialStep">
            <h1>Kurumsal Copilot Girişi</h1>
            <p class="subtitle">Kubernetes ajanlarınızı çalıştırmak için tek tıkla GitHub hesabınızı bağlayın.</p>
            <button class="btn" onclick="startAuth()">
                <svg viewBox="0 0 24 24" fill="currentColor">
                    <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z"/>
                </svg>
                GitHub ile Giriş Yap
            </button>
        </div>

        <div class="code-box" id="codeBox">
            <p style="font-size: 13px; color: var(--text-secondary);">1. Aşağıdaki tek kullanımlık onay kodunu kopyalayın:</p>
            <div class="user-code" id="userCode" onclick="copyCode()" title="Tıkla ve Kopyala">----</div>
            <p style="font-size: 11px; color: #60a5fa; text-align: center; margin-bottom: 16px;">(Koda tıklayarak kopyalayabilirsiniz)</p>
            
            <p style="font-size: 13px; color: var(--text-secondary); margin-bottom: 10px;">2. GitHub onay sayfasında kodu yapıştırın:</p>
            <a class="btn" id="verifyBtn" href="#" target="_blank" style="background:#2563eb; color:#fff;">
                GitHub'da Onayla ↗
            </a>

            <div style="margin-top: 16px; font-size: 12px; color: var(--text-secondary); text-align: center;" id="statusText">
                <span class="spinner"></span> GitHub onayı bekleniyor... (Otomatik algılanacak)
            </div>
        </div>

        <!-- YENİ: SIFIR KOMUT, TAM OTOMATİK BAŞARI EKRANI -->
        <div class="success-box" id="successBox">
            <div class="status-badge">
                <svg width="20" height="20" viewBox="0 0 20 20" fill="currentColor">
                    <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd" />
                </svg>
                <span><strong>ModelConfig Cluster'a Tanımlandı!</strong><br><small style="color:#a7f3d0;">Herhangi bir komut çalıştırmanıza gerek yoktur.</small></span>
            </div>

            <div class="detail-item">
                <div class="detail-title">Kullanıcı Hesabı</div>
                <div class="detail-value" id="userDisplay">@...</div>
            </div>

            <div class="detail-item">
                <div class="detail-title">Cluster Üzerindeki Model Adınız</div>
                <div class="detail-value" id="modelDisplay">copilot-...</div>
            </div>

            <p style="font-size: 13px; color: var(--text-secondary); line-height: 1.5; margin-top: 14px;">
                🚀 <strong>Kagent UI</strong> sayfasına dönüp ajanınızın model ayarlarından <code id="modelInline" style="color:#60a5fa;">copilot-...</code> modelini seçerek hemen çalıştırabilirsiniz.
            </p>
        </div>
    </div>

    <script>
        let pollTimer = null;

        async function startAuth() {
            const btn = document.querySelector('#initialStep button');
            btn.innerHTML = '<span class="spinner"></span> Kod alınıyor...';
            btn.disabled = true;

            try {
                const res = await fetch('/api/auth/start', { method: 'POST' });
                const data = await res.json();
                
                document.getElementById('initialStep').style.display = 'none';
                document.getElementById('codeBox').style.display = 'block';
                document.getElementById('userCode').innerText = data.user_code;
                document.getElementById('verifyBtn').href = data.verification_uri;

                window.open(data.verification_uri, '_blank');

                pollStatus(data.device_code, data.interval || 5);
            } catch (e) {
                alert('Giriş oturumu başlatılamadı: ' + e);
                btn.disabled = false;
                btn.innerText = 'GitHub ile Giriş Yap';
            }
        }

        function copyCode() {
            const code = document.getElementById('userCode').innerText;
            navigator.clipboard.writeText(code);
            const uc = document.getElementById('userCode');
            const orig = uc.innerText;
            uc.innerText = 'KOPYALANDI!';
            setTimeout(() => { uc.innerText = orig; }, 1200);
        }

        function pollStatus(deviceCode, interval) {
            pollTimer = setInterval(async () => {
                try {
                    const res = await fetch(`/api/auth/poll?device_code=${deviceCode}`);
                    const data = await res.json();
                    if (data.status === 'success') {
                        clearInterval(pollTimer);
                        document.getElementById('codeBox').style.display = 'none';
                        document.getElementById('successBox').style.display = 'block';
                        
                        const modelName = `copilot-${data.username.toLowerCase()}`;
                        document.getElementById('userDisplay').innerText = `@${data.username}`;
                        document.getElementById('modelDisplay').innerText = modelName;
                        document.getElementById('modelInline').innerText = modelName;
                    } else if (data.status === 'error') {
                        document.getElementById('statusText').innerText = 'Hata: ' + data.error;
                    }
                } catch (e) {}
            }, Math.max(interval, 4) * 1000);
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
            self.wfile.write(INDEX_HTML.encode("utf-8"))

        elif url.path == "/api/auth/poll":
            qs = urllib.parse.parse_qs(url.query)
            device_code = qs.get("device_code", [None])[0]
            if not device_code:
                self.send_response(400); self.end_headers(); return

            token_url = "https://github.com/login/oauth/access_token"
            data = {
                "client_id": COPILOT_CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code"
            }
            try:
                resp = requests.post(token_url, json=data, headers={"Accept": "application/json", "User-Agent": "KagentGateway/1.0"}, timeout=10)
                token_info = resp.json()
            except Exception as pe:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "pending", "debug": str(pe)}).encode())
                return

            if "access_token" in token_info:
                oauth_token = token_info["access_token"]
                
                try:
                    u_resp = requests.get("https://api.github.com/user", 
                                          headers={"Authorization": f"token {oauth_token}", "User-Agent": "KagentGateway/1.0"}, timeout=10)
                    username = u_resp.json().get("login", "unknown")

                    copilot_data = fetch_copilot_token(oauth_token)
                    api_key = f"sk-kagent-{secrets.token_hex(16)}"

                    session_data = {
                        "username": username,
                        "api_key": api_key,
                        "github_token": oauth_token,
                        "copilot_token": copilot_data["token"],
                        "expires_at": copilot_data["expires_at"]
                    }
                    save_user_session(username, session_data)
                    USER_SESSIONS[api_key] = session_data

                    print(f"[Auth Success & Auto-Provisioned] Kullanici: {username}", flush=True)

                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "success", "username": username, "api_key": api_key}).encode())
                except Exception as ex:
                    print(f"[Auth Exchange Error] {ex}", flush=True)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "error", "error": str(ex)}).encode())
            else:
                err = token_info.get("error", "authorization_pending")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "pending", "error": err}).encode())

        elif url.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)

        if url.path == "/api/auth/start":
            resp = requests.post(
                "https://github.com/login/device/code",
                json={"client_id": COPILOT_CLIENT_ID, "scope": "read:user"},
                headers={"Accept": "application/json", "User-Agent": "KagentGateway/1.0"},
                timeout=10
            )
            data = resp.json()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        elif self.path.startswith("/v1/chat/completions"):
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
            headers = {
                "Authorization": f"Bearer {session['copilot_token']}",
                "Content-Type": "application/json",
                "User-Agent": COPILOT_HEADERS["User-Agent"],
                "Editor-Version": COPILOT_HEADERS["Editor-Version"],
                "Editor-Plugin-Version": COPILOT_HEADERS["Editor-Plugin-Version"],
                "Openai-Intent": "conversation-panel"
            }

            try:
                with requests.post(upstream_url, data=body, headers=headers, stream=True) as resp:
                    self.send_response(resp.status_code)
                    for k, v in resp.headers.items():
                        if k.lower() not in ["content-length", "transfer-encoding", "content-encoding"]:
                            self.send_header(k, v)
                    self.end_headers()
                    for chunk in resp.iter_content(chunk_size=1024):
                        if chunk:
                            self.wfile.write(chunk)
            except Exception as e:
                self.send_response(502)
                self.end_headers()
                self.wfile.write(str(e).encode())

if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), MultiTenantHandler)
    print(f"Multi-Tenant Gateway aktif: http://0.0.0.0:{PORT}", flush=True)
    server.serve_forever()
