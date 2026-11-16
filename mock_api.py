#!/usr/bin/env python3
"""Servidor local que imita a API de validação de cupom, para testar o script.

- POST /v1/events/<id>/tickets : valida o código
- GET  /v1/events/<id>         : devolve os dados do evento (organization = produtora)

Códigos especiais: começando com RL -> HTTP 429 (rate limit); com ERR -> HTTP 500.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VALIDOS = {"ROMAEVENTOS": 50, "HYPE24HSINTRO": 30}
PRODUTORA = {"id": "c113d324-0000-0000-0000-000000000000", "name": "Produtora Teste"}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, obj, status=200):
        resp = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def do_GET(self):
        partes = self.path.strip("/").split("/")
        if len(partes) == 3 and partes[:2] == ["v1", "events"]:
            self._json({"data": {
                "id": partes[2], "name": "Evento Teste", "slug": "evento-teste",
                "organization": PRODUTORA, "has_coupon": True, "has_passkey": True,
                "place": {"name": "Local de Teste"},
            }})
            return
        self.send_response(404)
        self.send_header("content-length", "0")
        self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            body = {}
        code = (body.get("code") or "").upper()

        if code.startswith("RL"):
            resp = b"error code: 1015"
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
        self._json({"data": data})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8765), H).serve_forever()
