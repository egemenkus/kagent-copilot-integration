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

# Bellek içi oturum havuzu: { api_key: { "username": ..., "copilot_token": ..., "expires_at": ... } }
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
    # 1. RAM /dev/shm üzerine atomik yaz
    user_file = os.path.join(SHM_DIR, f"{username}.json")
    tmp_file = f"{user_file}.tmp"
    with open(tmp_file, "w") as f:
        json.dump(data, f)
    os.replace(tmp_file, user_file)

    # 2. Kubernetes Secret olarak kalıcılaştır (copilot-user-<username>)
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
        if e.code == 409: # Zaten varsa güncelle
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
    """Her kullanıcının Copilot token'ını süresi dolmadan arka planda yeniler."""
    while True:
        try:
            for user_file in os.listdir(SHM_DIR):
                if not user_file.endswith(".json"): continue
                filepath = os.path.join(SHM_DIR, user_file)
                with open(filepath) as f:
                    data = json.load(f)
                
                # Token bitimine 5 dakika kaldıysa yenile
                if time.time() >= (data.get("expires_at", 0) - 300):
                    new_token = fetch_copilot_token(data["oauth_token"])
                    data["copilot_token"] = new_token["token"]
                    data["expires_at"] = new_token["expires_at"]
                    save_user_session(data["username"], data)
                    # Global havuzu güncelle
                    USER_SESSIONS[data["api_key"]] = data
                    print(f"[Rotator] {data['username']} için token yenilendi.", flush=True)
        except Exception as e:
            print(f"[Rotator Error] {e}", flush=True)
        time.sleep(60)

threading.Thread(target=background_rotator, daemon=True).start()

HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
    <title>Kagent Copilot Gateway - Login</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0d1117; color: #c9d1d9; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 32px; width: 420px; box-shadow: 0 8px 24px rgba(0,0,0,0.4); text-align: center; }
        h2 { margin-top: 0; color: #58a6ff; }
        .btn { display: inline-block; background: #238636; color: #fff; text-decoration: none; padding: 12px 20px; border-radius: 6px; font-weight: bold; margin-top: 20px; border: none; cursor: pointer; width: 85%; }
        .btn:hover { background: #2ea043; }
        .token-box { background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 12px; margin-top: 20px; word-break: break-all; font-family: monospace; color: #7ee787; text-align: left; }
    </style>
</head>
<body>
    <div class="card">
        <h2>Copilot AI Gateway</h2>
        <p>Kagent modellerine bağlanmak için kurumsal GitHub hesabınızla oturum açın.</p>
        <a class="btn" href="https://github.com/login/oauth/authorize?client_id=CLIENT_ID_PLACEHOLDER&scope=read:user,copilot&redirect_uri=REDIRECT_URI_PLACEHOLDER">
            GitHub ile Giriş Yap
        </a>
    </div>
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
            html = HTML_PAGE.replace("CLIENT_ID_PLACEHOLDER", CLIENT_ID).replace("REDIRECT_URI_PLACEHOLDER", REDIRECT_URI)
            self.wfile.write(html.encode())

        elif url.path == "/auth/callback":
            qs = urllib.parse.parse_qs(url.query)
            code = qs.get("code", [None])[0]
            if not code:
                self.send_response(400); self.end_headers(); self.wfile.write(b"Authorization code missing"); return

            # GitHub'dan OAuth Access Token takası yap
            token_url = "https://github.com/login/oauth/access_token"
            token_payload = json.dumps({"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "code": code}).encode()
            req = urllib.request.Request(token_url, data=token_payload, headers={"Accept": "application/json", "Content-Type": "application/json"})
            with urllib.request.urlopen(req) as resp:
                token_data = json.loads(resp.read().decode())
            oauth_token = token_data.get("access_token")

            # Kullanıcı adını al
            user_req = urllib.request.Request("https://api.github.com/user", headers={"Authorization": f"Bearer {oauth_token}", "User-Agent": "Gateway"})
            with urllib.request.urlopen(user_req) as resp:
                user_info = json.loads(resp.read().decode())
            username = user_info["login"]

            # Copilot Internal Token al
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

            # Başarılı ekranı ve üretilen API anahtarı
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            res_html = f"""<!DOCTYPE html><html><body style="font-family:sans-serif;background:#0d1117;color:#c9d1d9;display:flex;justify-content:center;align-items:center;height:100vh;">
            <div style="background:#161b22;padding:32px;border-radius:8px;border:1px solid #30363d;max-width:500px;">
                <h3 style="color:#7ee787;">Giriş Başarılı, Hoş Geldiniz @{username}!</h3>
                <p>Copilot yetkiniz doğrulandı. Kagent ModelConfig için oluşturulan kişisel API Anahtarınız:</p>
                <div style="background:#0d1117;padding:12px;color:#58a6ff;font-family:monospace;border-radius:4px;word-break:break-all;">{api_key}</div>
                <p style="font-size:12px;color:#8b949e;margin-top:10px;">Bu anahtar Kagent üzerinde size ait bağımsız model yapılandırmasında kullanılır.</p>
            </div></body></html>"""
            self.wfile.write(res_html.encode())

        elif url.path == "/healthz":
            self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        # OpenAI uyumlu LLM proxy uç noktası
        if self.path.startswith("/v1/chat/completions"):
            auth_header = self.headers.get("Authorization", "")
            token = auth_header.replace("Bearer ", "").strip()
            
            # API key'den oturumu çöz
            session = USER_SESSIONS.get(token)
            if not session:
                # Bellekte yoksa /dev/shm tara
                for f in os.listdir(SHM_DIR):
                    if f.endswith(".json"):
                        with open(os.path.join(SHM_DIR, f)) as sfile:
                            c = json.load(sfile)
                            if c.get("api_key") == token:
                                session = c
                                USER_SESSIONS[token] = session
                                break
            
            if not session:
                self.send_response(401); self.end_headers()
                self.wfile.write(json.dumps({"error": "Geçersiz veya süresi dolmuş Kagent API Anahtarı"}).encode())
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
                self.send_response(e.code); self.end_headers()
                self.wfile.write(e.read())

if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), MultiTenantHandler)
    print(f"Multi-Tenant Gateway çalışıyor: http://0.0.0.0:{PORT}", flush=True)
    server.serve_forever()
