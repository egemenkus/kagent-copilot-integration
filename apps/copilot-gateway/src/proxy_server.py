import http.server
import json
import os
import socketserver
import urllib.error
import urllib.request

TOKEN_FILE = "/dev/shm/session.json"
PORT = 8080


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/healthz":
            if os.path.exists(TOKEN_FILE):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")
            else:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"Waiting for Token")
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if not os.path.exists(TOKEN_FILE):
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": "Token not ready in memory"}}).encode())
            return

        try:
            with open(TOKEN_FILE, "r") as f:
                session = json.load(f)
            copilot_token = session.get("token")
            endpoints = session.get("endpoints", {})
            api_base = endpoints.get("api", "https://api.individual.githubcopilot.com")
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
            return

        content_length = int(self.headers.get("Content-Length", 0))
        req_body = self.rfile.read(content_length)

        upstream_url = f"{api_base}/chat/completions"
        upstream_headers = {
            "Authorization": f"Bearer {copilot_token}",
            "Content-Type": "application/json",
            "User-Agent": "GitHubCopilotChat/0.22.0",
            "Editor-Version": "vscode/1.90.0",
            "Copilot-Integration-Id": "vscode-chat",
        }

        req = urllib.request.Request(upstream_url, data=req_body, headers=upstream_headers, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ["content-length", "transfer-encoding", "content-encoding"]:
                        self.send_header(k, v)
                self.end_headers()
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except urllib.error.HTTPError as he:
            self.send_response(he.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(he.read())
        except Exception as ex:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(ex)}).encode())


def main():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), ProxyHandler) as httpd:
        print(f"[copilot-proxy] Proxy {PORT} portunda calisiyor.", flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
