"""SPEC-1 acceptance tests. Three fail on the stub; the coder makes them pass."""

from __future__ import annotations

import threading
from http.client import HTTPConnection
from http.server import HTTPServer

import pytest

from app.orders import COLUMNS, Order, TooManyRows, export_csv, sample_orders
from app.server import Handler


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


def test_content_type(server):
    conn = HTTPConnection("127.0.0.1", server.server_port)
    conn.request("GET", "/orders.csv")
    r = conn.getresponse()
    assert r.status == 200
    assert r.getheader("Content-Type", "").startswith("text/csv")


def test_header_row_when_empty():
    text = export_csv([])
    assert text.splitlines()[0] == ",".join(COLUMNS)


def test_size_limit():
    big = [Order(i, "c", 1, "2026-11-01") for i in range(10_001)]
    with pytest.raises(TooManyRows):
        export_csv(big)


def test_rows_follow_header():
    text = export_csv(sample_orders(2))
    lines = text.splitlines()
    assert lines[0] == ",".join(COLUMNS) and len(lines) == 3
    assert lines[1].startswith("1,acme-1,")
