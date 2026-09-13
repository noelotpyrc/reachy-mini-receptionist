"""Loopback-only UI preview with fabricated status. Never imports robot/service code."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

STATIC = Path(__file__).resolve().parents[1] / "src/reachy_mini_reception_app/static"


def sample(phase: str) -> dict:
    return {
        "phase": phase, "config_id": "clinic-candidate", "robot_id": "preview-robot",
        "run_id": "native-0123456789abcdef0123456789abcdef", "elapsed_s": 3812,
        "control_age_s": 0.1, "reason": "runtime_failed" if phase == "faulted" else None,
        "configuration": {"profile": "reviewed-profile", "tools": "time-web",
                          "vision_policy": "door-v4-20260827", "vision_runtime": "broker-v1",
                          "duration_s": None, "record_audio": True, "record_video": False, "capture_vision": True},
        "health": {"runtime_phase": phase, "event_loop_age_s": 0.2,
                   "audio": {"expected": True, "sequence": 190600, "age_s": 0.02},
                   "video": {"expected": True, "sequence": 57180, "age_s": 0.06}},
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        status_code = 200
        content_type = "text/html; charset=utf-8"
        if path == "/api/reception/status":
            query = parse_qs(urlsplit(self.headers.get("Referer", "")).query)
            phase = query.get("state", ["ready"])[0]
            if phase == "disconnected":
                status_code = 503
            data = sample(phase if phase in {"starting", "ready", "stopping", "stopped", "faulted"} else "ready")
            if phase == "stale":
                data["control_age_s"] = 9
            body = json.dumps(data).encode()
            content_type = "application/json"
        elif path == "/":
            body = (STATIC / "index.html").read_bytes().replace(b"<main>", b'<main><p style="color:#865c12;margin-bottom:16px">OFFLINE PREVIEW</p>')
        elif path in {"/static/style.css", "/static/status.js", "/static/reachy-icon.png"}:
            body = (STATIC / Path(path).name).read_bytes()
            content_type = {".css": "text/css", ".js": "text/javascript", ".png": "image/png"}[Path(path).suffix]
        elif path == "/mobile":
            body = b'<title>Mobile preview</title><iframe title="Reception mobile preview" src="/" style="width:360px;height:900px;border:0"></iframe>'
        else:
            status_code, body = 404, b"Not found"
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Offline preview: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
