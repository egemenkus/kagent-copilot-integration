import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

TOKEN_FILE = "/dev/shm/session.json"
CLIENT_ID = "Iv1.b507a08c87ecfe98"
SECRET_NAME = "copilot-session-store"
NAMESPACE = "kagent"
K8S_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
K8S_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
K8S_API = "https://kubernetes.default.svc"


def log(msg: str) -> None:
    print(f"[auth-rotator] {time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}", flush=True)


def get_k8s_token() -> str:
    with open(K8S_TOKEN_PATH, "r") as f:
        return f.read().strip()


def k8s_request(method: str, path: str, body: dict = None) -> dict:
    token = get_k8s_token()
    ctx = ssl.create_default_context(cafile=K8S_CA_PATH)
    url = f"{K8S_API}{path}"
    data = json.dumps(body).encode() if body else None

    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, context=ctx) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def load_persisted_oauth() -> str:
    res = k8s_request("GET", f"/api/v1/namespaces/{NAMESPACE}/secrets/{SECRET_NAME}")
    if res and "data" in res and "oauth_token" in res["data"]:
        return base64.b64decode(res["data"]["oauth_token"]).decode()
    return None


def save_persisted_oauth(oauth_token: str) -> None:
    encoded = base64.b64encode(oauth_token.encode()).decode()
    body = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": SECRET_NAME, "namespace": NAMESPACE},
        "type": "Opaque",
        "data": {"oauth_token": encoded},
    }
    existing = k8s_request("GET", f"/api/v1/namespaces/{NAMESPACE}/secrets/{SECRET_NAME}")
    if existing:
        k8s_request("PUT", f"/api/v1/namespaces/{NAMESPACE}/secrets/{SECRET_NAME}", body)
    else:
        k8s_request("POST", f"/api/v1/namespaces/{NAMESPACE}/secrets", body)
    log("Kurumsal OAuth oturumu Kubernetes Secret icerisine kalici kaydedildi.")


def initiate_device_flow() -> dict:
    req = urllib.request.Request(
        "https://github.com/login/device/code",
        data=json.dumps({"client_id": CLIENT_ID, "scope": "read:user"}).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "GitHubCopilotChat/0.22.0",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def poll_for_token(device_code: str, interval: int) -> str:
    while True:
        time.sleep(interval)
        req = urllib.request.Request(
            "https://github.com/login/oauth/access_token",
            data=json.dumps(
                {
                    "client_id": CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "GitHubCopilotChat/0.22.0",
            },
        )
        with urllib.request.urlopen(req) as resp:
            res = json.loads(resp.read().decode())
        if "access_token" in res:
            return res["access_token"]
        if res.get("error") not in ["authorization_pending", "slow_down"]:
            raise RuntimeError(f"OAuth Hatasi: {res}")


def fetch_copilot_token(oauth_token: str) -> dict:
    req = urllib.request.Request(
        "https://api.github.com/copilot_internal/v2/token",
        headers={
            "Authorization": f"token {oauth_token}",
            "Accept": "application/json",
            "User-Agent": "GitHubCopilotChat/0.22.0",
            "Editor-Version": "vscode/1.90.0",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def main():
    gh_oauth = load_persisted_oauth()
    if gh_oauth:
        log("Kayitli kurumsal OAuth oturumu Secret'tan basariyla yuklendi. Onay gerekmiyor.")
    else:
        log("================================================================")
        log("ILK KURULUM: TEK SEFERLIK KURUMSAL SSO ONAYI GEREKLI")
        dev = initiate_device_flow()
        log(f"1. Tarayicida acin: {dev.get('verification_uri')}")
        log(f"2. Kodu girin:     {dev.get('user_code')}")
        log("================================================================")
        gh_oauth = poll_for_token(dev["device_code"], max(5, dev.get("interval", 5)))
        save_persisted_oauth(gh_oauth)

    while True:
        try:
            token_data = fetch_copilot_token(gh_oauth)
            tmp_file = TOKEN_FILE + ".tmp"
            with open(tmp_file, "w") as f:
                json.dump(token_data, f)
            os.replace(tmp_file, TOKEN_FILE)
            os.chmod(TOKEN_FILE, 0o600)

            expires_at = token_data.get("expires_at", time.time() + 1800)
            sleep_duration = max(60, expires_at - time.time() - 900)
            log(f"Token RAM uzerinde guncellendi. Sonraki proaktif yenileme: {int(sleep_duration)} sn sonra.")
            time.sleep(sleep_duration)
        except Exception as err:
            log(f"Yenileme sirasinda hata: {err}. 15 sn sonra tekrar denenecek...")
            time.sleep(15)


if __name__ == "__main__":
    main()
