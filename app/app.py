#!/usr/bin/env python3
import base64
import binascii
import html
import json
import os
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

APP_HOST = os.environ.get("APP_HOST", "0.0.0.0")
APP_PORT = int(os.environ.get("APP_PORT", "8080"))
APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "change-this-password")
AUTH_OUTPUT_PATH = os.environ.get("AUTH_OUTPUT_PATH", "/output/auth.json")
STATE_TTL_SECONDS = int(os.environ.get("STATE_TTL_SECONDS", "900"))
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", "131072"))

STATE = {}
STATE_LOCK = threading.Lock()


def page(title: str, body: str) -> bytes:
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #101214;
      --card: #191d21;
      --border: #30363d;
      --text: #f0f3f6;
      --muted: #aeb7c2;
      --accent: #2f81f7;
      --ok: #2ea043;
      --error: #f85149;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 16px/1.5 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{
      width: min(920px, calc(100% - 32px));
      margin: 42px auto;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 24px;
      box-shadow: 0 12px 35px rgba(0,0,0,.25);
    }}
    h1 {{ margin: 0 0 8px; font-size: 30px; }}
    h2 {{ margin-top: 28px; }}
    p {{ color: var(--muted); }}
    label {{
      display: block;
      margin: 18px 0 7px;
      font-weight: 650;
    }}
    textarea, input[type=text] {{
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #0d1117;
      color: var(--text);
      padding: 12px;
      font: 14px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }}
    textarea {{ min-height: 118px; resize: vertical; }}
    .value {{
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      background: #0d1117;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 180px 1fr;
      gap: 10px 14px;
      align-items: start;
    }}
    .key {{ color: var(--muted); font-weight: 700; }}
    button, .button {{
      display: inline-block;
      margin-top: 22px;
      border: 0;
      border-radius: 8px;
      background: var(--accent);
      color: white;
      padding: 11px 17px;
      font-weight: 750;
      cursor: pointer;
      text-decoration: none;
    }}
    .secondary {{ background: #30363d; margin-left: 8px; }}
    .success {{
      border-left: 4px solid var(--ok);
      background: rgba(46,160,67,.12);
      padding: 12px 14px;
      border-radius: 6px;
      color: #d7f5dc;
    }}
    .error {{
      border-left: 4px solid var(--error);
      background: rgba(248,81,73,.12);
      padding: 12px 14px;
      border-radius: 6px;
      color: #ffd7d5;
    }}
    code {{ color: #d2a8ff; }}
    @media (max-width: 650px) {{
      .grid {{ grid-template-columns: 1fr; }}
      .key {{ margin-top: 8px; }}
    }}
  </style>
</head>
<body>
<main>
  <div class="card">
    {body}
  </div>
</main>
</body>
</html>"""
    return document.encode("utf-8")


def decode_jwt(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("The access token is not a normal three-section JWT.")

    payload = parts[1]
    payload += "=" * (-len(payload) % 4)

    try:
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError, binascii.Error) as exc:
        raise ValueError("The JWT payload could not be decoded as JSON.") from exc

    if not isinstance(data, dict):
        raise ValueError("The JWT payload is not a JSON object.")

    return data


def first_value(data: dict, names: tuple[str, ...]):
    for name in names:
        value = data.get(name)
        if value is not None and value != "":
            return value
    return None


def readable_expiration(value) -> str:
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return "Could not convert to a UTC date"


def purge_expired_state() -> None:
    cutoff = time.time() - STATE_TTL_SECONDS
    with STATE_LOCK:
        expired = [key for key, item in STATE.items() if item["created"] < cutoff]
        for key in expired:
            STATE.pop(key, None)


def atomic_write_auth(data: dict) -> None:
    output = os.path.abspath(AUTH_OUTPUT_PATH)
    directory = os.path.dirname(output)
    os.makedirs(directory, exist_ok=True)

    if os.path.lexists(output) and not os.path.isfile(output):
        raise RuntimeError("auth.json exists but is not a normal file. It was not replaced.")

    encoded = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    fd, temporary_path = tempfile.mkstemp(prefix=".auth.json.", dir=directory)

    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o600)

        os.replace(temporary_path, output)

        directory_fd = os.open(directory, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


class Handler(BaseHTTPRequestHandler):
    server_version = "TidarrTokenHelper/1.0"

    def log_message(self, fmt, *args):
        # Never log request bodies or token values.
        super().log_message(fmt, *args)

    def send_common_headers(self, content_type="text/html; charset=utf-8"):
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )

    def authorized(self) -> bool:
        if not APP_PASSWORD:
            return True

        supplied = self.headers.get("Authorization", "")
        expected = base64.b64encode(
            f"{APP_USERNAME}:{APP_PASSWORD}".encode("utf-8")
        ).decode("ascii")

        return secrets.compare_digest(supplied, f"Basic {expected}")

    def require_auth(self) -> bool:
        if self.authorized():
            return False

        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Tidarr Token Helper"')
        self.send_common_headers()
        self.end_headers()
        self.wfile.write(page("Authentication required", "<h1>Authentication required</h1>"))
        return True

    def read_form(self) -> dict[str, list[str]]:
        length_text = self.headers.get("Content-Length", "0")
        try:
            length = int(length_text)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length header.") from exc

        if length <= 0:
            raise ValueError("The submitted form was empty.")
        if length > MAX_BODY_BYTES:
            raise ValueError("The submitted form is too large.")

        raw = self.rfile.read(length)
        return parse_qs(raw.decode("utf-8"), keep_blank_values=True)

    def respond(self, status: HTTPStatus, content: bytes, content_type="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_common_headers(content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.path == "/health":
            content = b"ok\n"
            self.respond(HTTPStatus.OK, content, "text/plain; charset=utf-8")
            return

        if self.require_auth():
            return

        if self.path != "/":
            self.respond(
                HTTPStatus.NOT_FOUND,
                page("Not found", '<h1>Not found</h1><a class="button secondary" href="/">Home</a>'),
            )
            return

        body = """
<h1>Tidarr Token Helper</h1>
<p>Paste the access token and refresh token. The JWT is decoded locally inside this container. Nothing is sent to an external service.</p>
<form method="post" action="/generate" autocomplete="off">
  <label for="token">Access token</label>
  <textarea id="token" name="token" required spellcheck="false"></textarea>

  <label for="refresh_token">Refresh token</label>
  <textarea id="refresh_token" name="refresh_token" required spellcheck="false"></textarea>

  <button type="submit">Generate</button>
</form>
"""
        self.respond(HTTPStatus.OK, page("Tidarr Token Helper", body))

    def do_POST(self):
        if self.require_auth():
            return

        purge_expired_state()

        try:
            if self.path == "/generate":
                self.handle_generate()
                return

            if self.path.startswith("/create/"):
                self.handle_create(self.path.removeprefix("/create/"))
                return

            self.respond(
                HTTPStatus.NOT_FOUND,
                page("Not found", '<h1>Not found</h1><a class="button secondary" href="/">Home</a>'),
            )
        except ValueError as exc:
            self.respond(
                HTTPStatus.BAD_REQUEST,
                page(
                    "Invalid token",
                    f'<h1>Could not generate details</h1><div class="error">{html.escape(str(exc))}</div>'
                    '<a class="button secondary" href="/">Try again</a>',
                ),
            )
        except Exception as exc:
            self.respond(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                page(
                    "Error",
                    f'<h1>Operation failed</h1><div class="error">{html.escape(str(exc))}</div>'
                    '<a class="button secondary" href="/">Home</a>',
                ),
            )

    def handle_generate(self):
        form = self.read_form()
        token = form.get("token", [""])[0].strip()
        refresh_token = form.get("refresh_token", [""])[0].strip()

        if not token:
            raise ValueError("Access token is required.")
        if not refresh_token:
            raise ValueError("Refresh token is required.")

        claims = decode_jwt(token)

        expires_at = first_value(claims, ("exp",))
        user_id = first_value(claims, ("userId", "user_id", "uid", "userid", "sub"))
        country_code = first_value(
            claims,
            ("cc", "countryCode", "country_code", "country"),
        )

        missing = []
        if expires_at is None:
            missing.append("exp / expires_at")
        if user_id is None:
            missing.append("user ID")
        if country_code is None:
            missing.append("cc / country code")
        if missing:
            raise ValueError("Missing required JWT value(s): " + ", ".join(missing))

        auth_data = {
            "token": token,
            "refresh_token": refresh_token,
            "expires_at": str(expires_at),
            "user_id": str(user_id),
            "country_code": str(country_code),
        }

        state_id = secrets.token_urlsafe(24)
        with STATE_LOCK:
            STATE[state_id] = {"created": time.time(), "data": auth_data}

        body = f"""
<h1>Generated details</h1>
<p>Review the values below. Press <strong>Create auth.json</strong> to replace the existing file.</p>

<h2>Token</h2>
<div class="value">{html.escape(token)}</div>

<h2>Refresh token</h2>
<div class="value">{html.escape(refresh_token)}</div>

<h2>Decoded values</h2>
<div class="grid">
  <div class="key">User ID</div>
  <div class="value">{html.escape(str(user_id))}</div>

  <div class="key">Expires at</div>
  <div class="value">{html.escape(str(expires_at))}<br>{html.escape(readable_expiration(expires_at))}</div>

  <div class="key">Country code</div>
  <div class="value">{html.escape(str(country_code))}</div>
</div>

<form method="post" action="/create/{html.escape(state_id)}">
  <button type="submit">Create auth.json</button>
  <a class="button secondary" href="/">Cancel</a>
</form>
"""
        self.respond(HTTPStatus.OK, page("Generated details", body))

    def handle_create(self, state_id: str):
        if not state_id:
            raise ValueError("Missing generated result ID.")

        with STATE_LOCK:
            item = STATE.pop(state_id, None)

        if item is None:
            raise ValueError("This generated result expired or was already used. Generate it again.")

        if time.time() - item["created"] > STATE_TTL_SECONDS:
            raise ValueError("This generated result expired. Generate it again.")

        atomic_write_auth(item["data"])

        stat_result = os.stat(AUTH_OUTPUT_PATH)
        body = f"""
<h1>auth.json created</h1>
<div class="success">
  The file was written successfully to:<br>
  <code>{html.escape(AUTH_OUTPUT_PATH)}</code>
</div>
<div class="grid" style="margin-top:20px">
  <div class="key">Owner UID:GID</div>
  <div class="value">{stat_result.st_uid}:{stat_result.st_gid}</div>

  <div class="key">Permissions</div>
  <div class="value">{oct(stat_result.st_mode & 0o777)}</div>
</div>
<p>The owner should match the UID:GID configured by the <code>user:</code> setting in your Compose file.</p>
<a class="button" href="/">Generate another</a>
"""
        self.respond(HTTPStatus.OK, page("auth.json created", body))


def main():
    print(
        f"Tidarr Token Helper listening on http://{APP_HOST}:{APP_PORT} "
        f"as UID:GID {os.getuid()}:{os.getgid()}"
    )
    print(f"auth.json output path: {AUTH_OUTPUT_PATH}")
    server = ThreadingHTTPServer((APP_HOST, APP_PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
