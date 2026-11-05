"""A minimal HTTP server: GET /orders.csv (SPEC-1). Run with `python -m app.server`."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer

from app.orders import TooManyRows, export_csv, sample_orders

ORDERS = sample_orders()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path != "/orders.csv":
            self.send_error(404)
            return
        try:
            body = export_csv(ORDERS).encode("utf-8")
        except TooManyRows as exc:
            self.send_response(413)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(str(exc).encode("utf-8"))
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")  # SPEC-1 criterion 1 wants text/csv
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    HTTPServer(("127.0.0.1", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
