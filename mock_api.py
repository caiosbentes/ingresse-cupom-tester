#!/usr/bin/env python3
"""Servidor local que imita a API de validação de cupom, para testar o script."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VALIDOS = {"ROMAEVENTOS": 50, "HYPE24HSINTRO": 30}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            body = {}
        code = (body.get("code") or "").upper()

        if code.startswith("RL"):
            resp = b'error code: 1015'
            self.send_response(429)
            self.send_header("retry-after", "60")
            self.send_header("content-type", "text/plain")
            self.send_header("content-length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return
        if code.startswith("ERR"):
            self.send_response(500)
            self.send_header("content-length", "0")
            self.end_headers()
            return

        if code in VALIDOS:
            data = {"coupon": {"code": code, "discount_amount": VALIDOS[code],
                               "discount_type": "percentage", "current_usages": 1,
                               "max_usages": 100, "max_uses_per_user": 2,
                               "status": "active"}, "sessions": []}
        else:
            data = {"extras": {"code": "BFRULE020",
                               "message": "Passkey does not match any available ticket"},
                    "sessions": []}
        resp = json.dumps({"data": data}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8765), H).serve_forever()
