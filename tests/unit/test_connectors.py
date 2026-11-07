"""The connectors against fakes that mirror the documented endpoints: HubSpot, Dropbox Sign,
Google Drive and Slides."""

from __future__ import annotations

import json

import httpx
import pytest

from sindarin.connect import ConnectorError
from sindarin.crm import HubSpot
from sindarin.esign import DropboxSign, order_form_pdf
from sindarin.gdrive import GoogleDocs, Slide


class _HubSpot:
    def __init__(self) -> None:
        self.companies: dict[str, dict] = {}
        self.deals: dict[str, dict] = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer pat-test"
        body = json.loads(req.content) if req.content else {}
        p, m = req.url.path, req.method
        if p == "/account-info/v3/details":
            return httpx.Response(200, json={"portalId": 4242})
        if p == "/crm/v3/objects/companies/search":
            name = body["filterGroups"][0]["filters"][0]["value"]
            rows = [c for c in self.companies.values() if c["properties"]["name"] == name]
            return httpx.Response(200, json={"results": rows})
        if m == "POST" and p == "/crm/v3/objects/companies":
            cid = str(100 + len(self.companies))
            self.companies[cid] = {"id": cid, "properties": body["properties"]}
            return httpx.Response(201, json=self.companies[cid])
        if p == "/crm/v3/objects/deals/search":
            name = body["filterGroups"][0]["filters"][0]["value"]
            rows = [d for d in self.deals.values() if d["properties"]["dealname"] == name]
            return httpx.Response(200, json={"results": rows})
        if m == "POST" and p == "/crm/v3/objects/deals":
            did = str(900 + len(self.deals))
            self.deals[did] = {
                "id": did,
                "properties": body["properties"],
                "associations": body.get("associations"),
            }
            return httpx.Response(201, json=self.deals[did])
        if m == "PATCH" and p.startswith("/crm/v3/objects/deals/"):
            d = self.deals[p.rsplit("/", 1)[1]]
            d["properties"].update(body["properties"])
            return httpx.Response(200, json=d)
        if m == "GET" and p.startswith("/crm/v3/objects/deals/"):
            return httpx.Response(200, json=self.deals[p.rsplit("/", 1)[1]])
        return httpx.Response(404, json={"message": p})


def test_hubspot_creates_moves_and_reads_back_the_deal():
    fake = _HubSpot()
    crm = HubSpot("pat-test", transport=httpx.MockTransport(fake))
    out = crm.upsert_deal(
        "D-2026-01", account="Contoso Ltd", product="Team", amount_cents=1_020_000, stage="open"
    )
    assert out == {
        "crm_deal_id": "900",
        "crm_stage": "decisionmakerboughtin",
        "crm_url": "https://app.hubspot.com/contacts/4242/record/0-3/900",
    }
    deal = fake.deals["900"]
    assert deal["properties"]["dealname"] == "D-2026-01 · Contoso Ltd · Team"
    assert (
        deal["properties"]["amount"] == "10200.00" and deal["properties"]["pipeline"] == "default"
    )
    assert (
        deal["associations"][0]["to"]["id"] == "100"
        and fake.companies["100"]["properties"]["name"] == "Contoso Ltd"
    )
    # won: the same deal moves, no second one is made; the company is found, not re-made
    out = crm.upsert_deal(
        "D-2026-01",
        account="Contoso Ltd",
        product="Team",
        amount_cents=1_020_000,
        stage="won",
        known_id="900",
    )
    assert out["crm_stage"] == "closedwon" and len(fake.deals) == 1 and len(fake.companies) == 1
    # a replay without the known id finds it by name
    out = crm.upsert_deal(
        "D-2026-01", account="Contoso Ltd", product="Team", amount_cents=1_020_000, stage="lost"
    )
    assert (
        out["crm_deal_id"] == "900" and fake.deals["900"]["properties"]["dealstage"] == "closedlost"
    )
    with pytest.raises(ConnectorError, match="unknown stage"):
        crm.upsert_deal("D-2026-01", account="x", product="y", amount_cents=1, stage="maybe")


class _DropboxSign:
    def __init__(self) -> None:
        self.requests: dict[str, dict] = {}
        self.last_body = b""

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"].startswith("Basic ")
        p, m = req.url.path, req.method
        if m == "POST" and p == "/v3/signature_request/send":
            self.last_body = req.content
            rid = f"sr_{len(self.requests) + 1}"
            self.requests[rid] = {
                "signature_request_id": rid,
                "is_complete": False,
                "is_declined": False,
                "signing_url": None,
                "signatures": [
                    {
                        "signer_email_address": "cfo@contoso.example",
                        "status_code": "awaiting_signature",
                    }
                ],
            }
            return httpx.Response(200, json={"signature_request": self.requests[rid]})
        if m == "GET" and p.startswith("/v3/signature_request/"):
            return httpx.Response(
                200, json={"signature_request": self.requests[p.rsplit("/", 1)[1]]}
            )
        return httpx.Response(404, json={"error": {"error_msg": p}})

    def sign(self, rid: str) -> None:
        r = self.requests[rid]
        r["is_complete"] = True
        r["signatures"][0].update(status_code="signed", signed_at=1762560000)


def test_dropbox_sign_sends_the_order_form_in_test_mode_and_sees_it_signed():
    pdf = order_form_pdf(
        "D-2026-01",
        account="Contoso Ltd",
        product="Team",
        price_cents=1_020_000,
        list_cents=1_200_000,
        discount_bps=1500,
        currency="USD",
        period="2026-11",
    )
    assert pdf[:5] == b"%PDF-"
    fake = _DropboxSign()
    esign = DropboxSign(
        "key", signer_email="cfo@contoso.example", transport=httpx.MockTransport(fake)
    )
    out = esign.send_order_form("D-2026-01", account="Contoso Ltd", pdf=pdf)
    assert out["signature_request_id"] == "sr_1" and out["signer"] == "cfo@contoso.example"
    assert out["test_mode"] is True
    body = fake.last_body
    assert b'name="test_mode"\r\n\r\n1\r\n' in body
    assert b'name="metadata[deal]"\r\n\r\nD-2026-01\r\n' in body
    assert b'name="signers[0][email_address]"\r\n\r\ncfo@contoso.example' in body
    assert b'filename="D-2026-01-order-form.pdf"' in body and b"%PDF-" in body
    assert esign.status("sr_1") == {
        "complete": False,
        "signed_by": "",
        "signed_at": "",
        "declined": False,
    }
    fake.sign("sr_1")
    seen = esign.status("sr_1")
    assert (
        seen["complete"]
        and seen["signed_by"] == "cfo@contoso.example"
        and seen["signed_at"].endswith("UTC")
    )


class _Google:
    def __init__(self) -> None:
        self.files: dict[str, dict] = {}
        self.batches: list[list[dict]] = []
        self.shared: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer tok"
        p, m, host = req.url.path, req.method, req.url.host
        if host == "www.googleapis.com" and p == "/drive/v3/files" and m == "GET":
            q = req.url.params["q"]
            name = q.split("name = '")[1].split("'")[0]
            rows = [
                {"id": k, "name": v["name"]} for k, v in self.files.items() if v["name"] == name
            ]
            return httpx.Response(200, json={"files": rows})
        if host == "www.googleapis.com" and p == "/drive/v3/files" and m == "POST":
            body = json.loads(req.content)
            fid = f"f{len(self.files) + 1}"
            self.files[fid] = body
            return httpx.Response(200, json={"id": fid})
        if host == "www.googleapis.com" and p.startswith("/upload/drive/v3/files"):
            meta = json.loads(req.content.split(b"\r\n\r\n")[1].split(b"\r\n--")[0])
            fid = (
                p.rsplit("/", 1)[1] if p != "/upload/drive/v3/files" else f"f{len(self.files) + 1}"
            )
            self.files[fid] = {**meta, "bytes": len(req.content)}
            return httpx.Response(
                200, json={"id": fid, "webViewLink": f"https://drive.google.com/file/d/{fid}/view"}
            )
        if host == "www.googleapis.com" and p.endswith("/permissions"):
            self.shared.append(p.split("/")[4])
            return httpx.Response(200, json={"id": "perm"})
        if host == "slides.googleapis.com" and m == "GET":
            return httpx.Response(200, json={"slides": [{"objectId": "default-slide"}]})
        if host == "slides.googleapis.com" and p.endswith(":batchUpdate"):
            self.batches.append(json.loads(req.content)["requests"])
            return httpx.Response(200, json={"replies": []})
        return httpx.Response(404, json={"error": {"message": f"{host}{p}"}})


def test_the_board_pack_lands_in_drive_and_slides():
    fake = _Google()
    docs = GoogleDocs("shared-folder", token=lambda: "tok", transport=httpx.MockTransport(fake))
    out = docs.board_pack(
        "2026-10",
        "October 2026",
        report_lines=["Revenue 93,885.00 USD across 13 invoices", "Costs 14,250.00 USD"],
        charts=[
            (
                "October 2026 — revenue by customer",
                b"\x89PNG...",
                "image/png",
                "revenue-2026-10.png",
            )
        ],
        exports=[("sales-2026-10.csv", b"invoice,amount\n", "text/csv")],
        report_text="**October 2026** · revenue",
    )
    assert out["files"] == 3 and out["slides"] == 3
    assert out["folder_link"] == "https://drive.google.com/drive/folders/f1"
    assert out["deck_link"] == "https://docs.google.com/presentation/d/f5/edit"
    folder = fake.files["f1"]
    assert folder["name"] == "Sindarin 2026-10" and folder["parents"] == ["shared-folder"]
    assert fake.files["f2"]["name"] == "revenue-2026-10.png" and fake.shared == ["f2"]
    assert {f["name"] for f in fake.files.values()} >= {"report-2026-10.md", "sales-2026-10.csv"}
    requests = fake.batches[-1]
    kinds = [next(iter(r)) for r in requests]
    assert kinds[0] == "createSlide" and kinds[-1] == "deleteObject"
    images = [r["createImage"] for r in requests if "createImage" in r]
    assert images[0]["url"] == "https://drive.google.com/uc?export=view&id=f2"
    texts = [r["insertText"]["text"] for r in requests if "insertText" in r]
    assert "October 2026 board pack" in texts and "The month" in texts
    # a second pack of the same month replaces the files and rebuilds the deck in place
    out2 = docs.board_pack("2026-10", "October 2026", report_lines=["x"], charts=[], exports=[])
    assert out2["deck_id"] == "f5" and out2["folder_id"] == "f1"


def test_connectors_refuse_to_start_without_their_credentials(tmp_path, monkeypatch):
    with pytest.raises(ConnectorError):
        HubSpot("")
    with pytest.raises(ConnectorError):
        DropboxSign("", signer_email="x@y")
    with pytest.raises(ConnectorError):
        DropboxSign("k", signer_email="")
    with pytest.raises(ConnectorError):
        GoogleDocs("")
    import google.auth

    def no_adc(**kw):
        raise RuntimeError("no default credentials here")

    monkeypatch.setattr(google.auth, "default", no_adc)
    with pytest.raises(ConnectorError, match="gcloud auth application-default login"):
        GoogleDocs("folder")
    assert Slide("t").body == ""
