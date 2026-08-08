"""The HTTP adapter, and mostly the four things standing in front of it.

This is a server that writes files to disk and fetches URLs, listening on the
machine where somebody's browser is. Everything it refuses is more interesting
than everything it serves, so that is what this file is about: the localhost
bind, the pinned `Host`, the `Origin` check, and the launch token. ADR 0018.

A real server on a real socket. The checks live in header handling, which is
exactly the part a mock would paper over.
"""

import http.client
import json
import threading

import pytest

import jobs
import server
from test_jobs import Harness


@pytest.fixture
def live(tmp_path):
    """A running server, wired to a Runner that never touches the network."""
    harness = Harness(tmp_path)
    httpd, url = server.build(str(tmp_path), port=0, token="test-token")
    httpd.RequestHandlerClass.runner = harness.runner
    # A short poll interval only so teardown is quick: `shutdown()` waits for
    # the current one to elapse, which at the default 0.5s is the entire
    # runtime of this file.
    thread = threading.Thread(target=httpd.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    client = Client(httpd.server_address[1], harness)
    yield client
    httpd.shutdown()
    httpd.server_close()


class Client:
    def __init__(self, port, harness):
        self.port = port
        self.harness = harness

    def request(self, method, path, body=None, token="test-token", host=None,
                origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token is not None:
            headers["X-Transkrp-Token"] = token
        if origin is not None:
            headers["Origin"] = origin
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, payload, headers)
        response = conn.getresponse()
        text = response.read().decode("utf-8")
        conn.close()
        ctype = response.getheader("Content-Type") or ""
        return response.status, (json.loads(text) if "json" in ctype else text)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body if body is not None else {}, **kw)

    def run(self, urls, **options):
        status, body = self.post("/api/runs", {"urls": urls, "options": options})
        assert status == 201, body
        self.harness.wait(type("R", (), {"id": body["id"]}))
        return body["id"]


# -- what it refuses ---------------------------------------------------------

def test_no_token_no_answer(live):
    status, body = live.get("/api/config", token=None)
    assert status == 401
    assert "token" in body["error"]["message"]


def test_a_wrong_token_is_refused(live):
    assert live.get("/api/config", token="not-the-token")[0] == 401


def test_the_token_is_checked_before_the_route_exists(live):
    """Otherwise the 404s map the API for anyone who can reach the port."""
    assert live.get("/api/runs/anything/at/all", token=None)[0] == 401


def test_a_rebinding_host_header_is_refused(live):
    """A page on the open web can point its own hostname at 127.0.0.1 and drive
    this server through a victim's browser. The giveaway is the Host header."""
    status, body = live.get("/api/config", host="transkrp.attacker.example")
    assert status == 403
    assert "localhost" in body["error"]["message"]


def test_localhost_by_any_of_its_names_is_fine(live):
    for host in (f"localhost:{live.port}", f"127.0.0.1:{live.port}", "localhost"):
        assert live.get("/api/config", host=host)[0] == 200


def test_another_origin_cannot_start_a_run(live):
    status, _ = live.post("/api/runs", {"urls": ["https://youtu.be/abcdefghijk"]},
                          origin="https://example.com")
    assert status == 403
    assert live.harness.fetched == []


def test_this_pages_own_origin_can(live):
    status, _ = live.post("/api/runs", {"urls": ["https://youtu.be/abcdefghijk"]},
                          origin=f"http://127.0.0.1:{live.port}")
    assert status == 201


def test_a_reply_never_invites_anything_else_in(live):
    """The page is self-contained; nothing here should be embeddable or be
    fetching from anywhere."""
    conn = http.client.HTTPConnection("127.0.0.1", live.port, timeout=5)
    conn.request("GET", "/?t=test-token")
    response = conn.getresponse()
    response.read()
    csp = response.getheader("Content-Security-Policy")
    conn.close()
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp


# -- what it serves ----------------------------------------------------------

def test_the_page_is_served_at_the_root(live):
    status, body = live.get("/?t=test-token", token=None)
    assert status == 200
    assert "<title>transkrp</title>" in body


def test_config_tells_the_page_where_files_go(live, tmp_path):
    status, body = live.get("/api/config")
    assert status == 200
    assert body["out_dir"] == str(tmp_path)
    assert body["defaults"] == jobs.DEFAULTS
    assert "large-v3" in body["whisper_models"]


def test_a_run_can_be_started_and_watched(live):
    run_id = live.run(["https://www.youtube.com/watch?v=abcdefghijk"])
    status, body = live.get(f"/api/runs/{run_id}")
    assert status == 200
    assert body["status"] == "done"
    assert body["items"][0]["title"] == "Talk 1"
    assert live.harness.fetched == ["https://www.youtube.com/watch?v=abcdefghijk"]


def test_runs_come_back_newest_first(live):
    first = live.run(["https://youtu.be/aaaaaaaaaaa"])
    second = live.run(["https://youtu.be/bbbbbbbbbbb"])
    _, body = live.get("/api/runs")
    assert [r["id"] for r in body["runs"]] == [second, first]


def test_a_finished_item_can_be_read_back(live):
    run_id = live.run(["https://youtu.be/abcdefghijk"])
    status, doc = live.get(f"/api/runs/{run_id}/items/0/document")
    assert status == 200 and doc.startswith("---")

    status, preview = live.get(f"/api/runs/{run_id}/items/0/preview")
    assert status == 200
    assert preview["paragraphs"][0]["anchor"].endswith("&t=0s")


def test_cancelling_over_http_reports_the_new_state(live):
    status, body = live.post("/api/runs", {"urls": ["https://youtu.be/abcdefghijk"]})
    status, body = live.post(f"/api/runs/{body['id']}/cancel")
    assert status == 200
    assert body["status"] in ("cancelled", "done")


# -- what it refuses to be confused by ---------------------------------------

def test_a_run_needs_at_least_one_url(live):
    status, body = live.post("/api/runs", {"urls": []})
    assert status == 400 and "at least one URL" in body["error"]["message"]


def test_a_body_that_is_not_an_object_is_refused(live):
    status, _ = live.post("/api/runs", ["https://youtu.be/abcdefghijk"])
    assert status == 400


def test_an_absurd_number_of_inputs_is_refused(live):
    status, body = live.post("/api/runs",
                             {"urls": [f"https://youtu.be/{i:011d}" for i in range(201)]})
    assert status == 400 and "playlist URL" in body["error"]["message"]


def test_an_oversized_body_is_not_read_into_memory(live):
    conn = http.client.HTTPConnection("127.0.0.1", live.port, timeout=5)
    conn.request("POST", "/api/runs", b"{}", {
        "Host": f"127.0.0.1:{live.port}", "X-Transkrp-Token": "test-token",
        "Content-Type": "application/json", "Content-Length": str(server.MAX_BODY + 1)})
    assert conn.getresponse().status == 400
    conn.close()


def test_an_unknown_run_is_a_404_not_a_crash(live):
    assert live.get("/api/runs/nope")[0] == 404
    assert live.get("/api/runs/nope/items/0/document")[0] == 404
    assert live.post("/api/runs/nope/cancel")[0] == 404


def test_an_item_index_off_the_end_is_a_404(live):
    run_id = live.run(["https://youtu.be/abcdefghijk"])
    assert live.get(f"/api/runs/{run_id}/items/99/document")[0] == 404
    assert live.get(f"/api/runs/{run_id}/items/notanumber/document")[0] == 404


def test_an_unknown_format_is_refused_before_rendering(live):
    run_id = live.run(["https://youtu.be/abcdefghijk"])
    status, body = live.get(f"/api/runs/{run_id}/items/0/document?format=exe")
    assert status == 400 and "Unknown format" in body["error"]["message"]


def test_unknown_routes_are_404(live):
    assert live.get("/api/nope")[0] == 404
    assert live.get("/../pyproject.toml")[0] == 404
    assert live.post("/api/config")[0] == 404


def test_the_browser_cannot_name_a_place_to_write(live, tmp_path):
    """The output directory is fixed at launch. Anything sent as an option is
    dropped by `_clean_options` rather than reaching the engine."""
    run_id = live.run(["https://youtu.be/abcdefghijk"], out_dir="/tmp/elsewhere",
                      proxy="http://attacker.example")
    _, body = live.get(f"/api/runs/{run_id}")
    assert body["out_dir"] == str(tmp_path)
    assert "out_dir" not in body["options"] and "proxy" not in body["options"]
    assert body["items"][0]["path"].startswith(str(tmp_path))
