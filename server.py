"""The HTTP adapter over `jobs.Runner`. Thin on purpose.

Everything hard is in `jobs.py`; this maps four routes onto it and serves one
page. That is the shape ADR 0018 chose so the eventual move into a larger
application is a rewrite of this file rather than of the engine.

Being a server that writes files and fetches URLs *on localhost* is the whole
security model, so it is enforced here rather than assumed:

- bound to 127.0.0.1, so nothing off this machine can reach it;
- the `Host` header is pinned to localhost, which is what stops a DNS-rebinding
  page from using a victim's browser as a proxy into it;
- `Origin` is checked on every write, so another origin's page cannot POST;
- a token minted at launch is required on every request, and travels in the URL
  the browser is opened with;
- documents are addressed by run id and item index. No path from the browser
  ever reaches the filesystem, and the output directory is fixed by `-o` at
  launch rather than typed into the page.
"""

from __future__ import annotations

import json
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import jobs
import transkrp
import ui

DEFAULT_PORT = 8765
# A POST here is a handful of URLs and a dozen flags. Anything larger is not a
# request this serves, and reading it into memory first is how you find out.
MAX_BODY = 1 << 20


class _Handler(BaseHTTPRequestHandler):
    # Keep-alive matters: the page polls twice a second, and a fresh connection
    # per poll would be the bulk of the work this server does.
    protocol_version = "HTTP/1.1"
    server_version = f"transkrp/{transkrp._version()}"

    runner: jobs.Runner
    token: str
    port: int

    # -- plumbing -----------------------------------------------------------

    def log_message(self, fmt, *args):
        """Silence. A 500ms poll would otherwise fill the terminal the user is
        reading the launch URL out of."""

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # This page is entirely self-contained; nothing here should ever be
        # fetching from, or embeddable by, anywhere else.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; "
                         "script-src 'unsafe-inline'; img-src data:; "
                         "connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def _fail(self, code: int, message: str, kind: str = "error") -> None:
        # Close rather than keep alive. A refusal often happens *before* the
        # request body is read — an oversized Content-Length is the whole point
        # of one of these — and leaving an unread body on a keep-alive socket
        # desynchronises the connection, so the next request gets parsed out of
        # the middle of the last one's payload.
        self.close_connection = True
        self._json({"error": {"kind": kind, "message": message}}, code)

    def _body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    # -- the checks ---------------------------------------------------------

    def _local(self) -> bool:
        """Is this addressed to localhost by name?

        A page on the open web can resolve its own hostname to 127.0.0.1 and
        make the victim's browser talk to this server — DNS rebinding. The
        request then arrives looking ordinary except for one thing: the `Host`
        header is the attacker's domain, because that is what the browser was
        told to fetch. Pinning it costs nothing and closes that door.
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("localhost", "127.0.0.1", "::1")

    def _same_origin(self) -> bool:
        """Writes must come from this page, not from another origin's."""
        origin = self.headers.get("Origin")
        if not origin:
            return True  # not a browser, or a same-origin GET-shaped POST
        host = urlparse(origin).hostname
        return urlparse(origin).port == self.port and host in ("localhost", "127.0.0.1", "::1")

    def _authorised(self, query: dict) -> bool:
        given = self.headers.get("X-Transkrp-Token") or (query.get("t") or [""])[0]
        return secrets.compare_digest(given, self.token)

    # -- routing ------------------------------------------------------------

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"

        if not self._local():
            return self._fail(403, "This server only answers to localhost.", "forbidden")
        if not self._authorised(query):
            return self._fail(401, "Wrong or missing token. Open the URL that "
                                   "transkrp printed when it started.", "unauthorised")
        if method == "POST" and not self._same_origin():
            return self._fail(403, "Cross-origin write refused.", "forbidden")

        parts = [p for p in path.split("/") if p]
        try:
            if method == "GET" and path == "/":
                return self._send(200, ui.PAGE.encode("utf-8"),
                                  "text/html; charset=utf-8")
            if method == "GET" and parts == ["api", "config"]:
                return self._json(self._config())
            if parts[:2] == ["api", "runs"]:
                return self._runs(method, parts[2:], query)
        except (IndexError, ValueError):
            pass
        self._fail(404, f"No route for {method} {path}.", "not_found")

    def _runs(self, method: str, rest: list[str], query: dict) -> None:
        runner = self.runner
        if not rest:
            if method == "GET":
                return self._json({"runs": runner.all_snapshots()})
            if method == "POST":
                return self._create()
        run_id, tail = rest[0], rest[1:]

        if method == "GET" and not tail:
            snap = runner.snapshot(run_id)
            return self._json(snap) if snap else self._fail(404, "No such run.", "not_found")
        if method == "POST" and tail == ["cancel"]:
            run = runner.cancel(run_id)
            return self._json(run.snapshot()) if run else self._fail(404, "No such run.", "not_found")
        # /api/runs/<id>/items/<n>/{document,preview}
        if method == "GET" and len(tail) == 3 and tail[0] == "items":
            if tail[2] == "document":
                return self._document(run_id, int(tail[1]), query)
            if tail[2] == "preview":
                got = runner.preview(run_id, int(tail[1]))
                return self._json(got) if got else self._fail(
                    404, "Nothing fetched there yet.", "not_found")
        self._fail(404, "No such route.", "not_found")

    def _create(self) -> None:
        body = self._body()
        if not isinstance(body, dict):
            return self._fail(400, "Expected a JSON object.", "bad_request")
        urls = [u.strip() for u in (body.get("urls") or []) if isinstance(u, str) and u.strip()]
        if not urls:
            return self._fail(400, "Give me at least one URL.", "bad_request")
        if len(urls) > 200:
            return self._fail(400, "That is more than 200 inputs; a playlist URL "
                                   "is a better way to ask for that many.", "bad_request")
        run = self.runner.submit(urls, body.get("options") or {})
        self._json(run.snapshot(), 201)

    def _document(self, run_id: str, index: int, query: dict) -> None:
        fmt = (query.get("format") or [None])[0]
        if fmt is not None and fmt not in ("md", "json", "srt", "vtt"):
            return self._fail(400, f"Unknown format {fmt!r}.", "bad_request")
        doc = self.runner.document(run_id, index, fmt)
        if doc is None:
            return self._fail(404, "Nothing fetched there yet.", "not_found")
        self._send(200, doc.encode("utf-8"), "text/plain; charset=utf-8")

    def _config(self) -> dict:
        """What the page needs to render itself without hardcoding this tool."""
        return {
            "version": transkrp._version(),
            "out_dir": self.runner.out_dir,
            "defaults": jobs.DEFAULTS,
            "whisper_models": list(jobs.WHISPER_MODELS),
            "proxy": bool(self.runner.proxy),
            "cookies": bool(self.runner.cookies),
        }


def build(out_dir: str = ".", port: int = DEFAULT_PORT, proxy: str | None = None,
          cookies: str | None = None, token: str | None = None):
    """The server and the URL to open it at, without starting it.

    Split from `serve` so the tests can drive a real server on an ephemeral port
    rather than mocking the one thing worth testing here, which is the checks.
    """
    runner = jobs.Runner(out_dir, proxy, cookies)
    token = token or secrets.token_urlsafe(16)

    class Handler(_Handler):
        pass

    Handler.runner = runner
    Handler.token = token
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        # Something already has 8765 — another copy of this, most likely. Take
        # any free port rather than refusing to start.
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    Handler.port = httpd.server_address[1]
    return httpd, f"http://127.0.0.1:{Handler.port}/?t={token}"


def serve(out_dir: str = ".", port: int = DEFAULT_PORT, proxy: str | None = None,
          cookies: str | None = None, open_browser: bool = True,
          out=None) -> int:
    out = out or sys.stderr
    httpd, url = build(out_dir, port, proxy, cookies)
    print(f"transkrp is serving at {url}", file=out)
    print(f"  writing to {out_dir}", file=out)
    print("  the token in that URL is what keeps other pages out; it changes "
          "every launch.\n  ctrl-c to stop.", file=out)
    if open_browser:
        # In a thread: on some desktops `webbrowser.open` blocks until the
        # browser process settles, and a server that isn't accepting yet is
        # exactly what the page it just opened will try to talk to.
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.", file=out)
    finally:
        httpd.server_close()
    return 0
