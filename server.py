import json
import os
import hashlib
import urllib.parse
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import request as urllib_request

ROOT = Path(__file__).resolve().parent
STORE_PATH = ROOT / ".quota_store.json"
FREE_TIER_LIMIT = int(os.environ.get("FREE_TIER_LIMIT", "10"))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def load_store():
    if not STORE_PATH.exists():
        return {"users": {}}
    try:
        return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"users": {}}


def save_store(store):
    STORE_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_user_id(user_id):
    if not user_id:
        return "anonymous"
    return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()


def get_usage(store, user_id):
    key = normalize_user_id(user_id)
    day = date.today().isoformat()
    user_entry = store["users"].get(key, {})
    if user_entry.get("day") != day:
        return {"day": day, "used": 0, "remaining": FREE_TIER_LIMIT}
    used = int(user_entry.get("used", 0))
    return {"day": day, "used": used, "remaining": max(0, FREE_TIER_LIMIT - used)}


def consume_usage(store, user_id):
    key = normalize_user_id(user_id)
    day = date.today().isoformat()
    user_entry = store["users"].get(key, {})
    if user_entry.get("day") != day:
        user_entry = {"day": day, "used": 0}
    used = int(user_entry.get("used", 0))
    if used >= FREE_TIER_LIMIT:
        return {"ok": False, "used": used, "remaining": 0}
    user_entry = {"day": day, "used": used + 1}
    store["users"][key] = user_entry
    save_store(store)
    return {"ok": True, "used": user_entry["used"], "remaining": max(0, FREE_TIER_LIMIT - user_entry["used"])}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/quota":
            user_id = urllib.parse.parse_qs(parsed.query).get("userId", [""])[0]
            store = load_store()
            usage = get_usage(store, user_id)
            self.send_json(200, {"ok": True, **usage})
            return
        if parsed.path == "/health":
            self.send_json(200, {"ok": True})
            return
        self.serve_static(parsed.path)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/consume":
            body = self.read_json_body()
            user_id = body.get("userId", "")
            store = load_store()
            result = consume_usage(store, user_id)
            self.send_json(200, result)
            return
        if parsed.path == "/api/ai":
            body = self.read_json_body()
            user_id = body.get("userId", "")
            store = load_store()
            usage = get_usage(store, user_id)
            if usage["remaining"] <= 0:
                self.send_json(429, {"ok": False, "error": "無料枠の上限に達しました。今日はこれ以上AIを使えません。", **usage})
                return
            consume_usage(store, user_id)
            prompt = body.get("prompt", "")
            api_key = body.get("apiKey") or ANTHROPIC_API_KEY
            if not api_key:
                self.send_json(400, {"ok": False, "error": "APIキーが設定されていません。"})
                return
            req = urllib_request.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps({
                    "model": body.get("model", "claude-sonnet-4-6"),
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": prompt}],
                }).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "anthropic-dangerous-direct-browser-access": "true",
                },
                method="POST",
            )
            try:
                with urllib_request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    text = "\n".join(
                        part.get("text", "") for part in (data.get("content") or []) if part.get("type") == "text"
                    )
                    self.send_json(200, {"ok": True, "text": text, **get_usage(load_store(), user_id)})
            except Exception as exc:
                self.send_json(502, {"ok": False, "error": str(exc)})
            return
        self.send_error(404)

    def read_json_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            return json.loads(data or "{}")
        except Exception:
            return {}

    def serve_static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        file_path = (ROOT / path.lstrip("/")).resolve()
        if not str(file_path).startswith(str(ROOT)):
            self.send_error(403)
            return
        if file_path.exists() and file_path.is_file():
            content = file_path.read_bytes()
            self.send_response(200)
            if file_path.suffix == ".html":
                self.send_header("Content-Type", "text/html; charset=utf-8")
            elif file_path.suffix == ".js":
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
            else:
                self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_error(404)

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
    print("Serving AI Office on http://0.0.0.0:8000")
    server.serve_forever()
