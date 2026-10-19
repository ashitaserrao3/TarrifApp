"""A stand-in for Supabase Storage's object download / upload, for tests."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "test-secret-key"


class FakeSupabase:
    def __init__(self, port=0):
        self.objects, self.uploads = {}, 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code, body, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.end_headers()
                self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

            def _ok(self):
                return self.headers.get("apikey") == KEY and self.headers.get("Authorization") == f"Bearer {KEY}"

            def do_GET(self):
                if not self._ok():
                    return self._send(400, {"statusCode": "403", "error": "Unauthorized", "message": "Invalid key"})
                if self.path not in fake.objects:
                    return self._send(400, {"statusCode": "404", "error": "not_found", "message": "Object not found"})
                self._send(200, fake.objects[self.path], "application/zip")

            def do_POST(self):
                data = self.rfile.read(int(self.headers["Content-Length"]))
                if not self._ok():
                    return self._send(400, {"statusCode": "403", "error": "Unauthorized", "message": "Invalid key"})
                if self.path in fake.objects and self.headers.get("x-upsert") != "true":
                    return self._send(400, {"statusCode": "409", "error": "Duplicate"})
                fake.objects[self.path] = data
                fake.uploads += 1
                self._send(200, {"Key": self.path})

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


if __name__ == "__main__":  # run it on a fixed port, e.g. to try the app by hand
    import sys
    FakeSupabase(port=int(sys.argv[1]))
    threading.Event().wait()
