"""HubSpot: the deal the room approves is the deal the sales team sees.

A proposed deal is created in HubSpot at an open stage, associated with its company; the
CFO's yes moves it to closed won at the approved amount, a deny to closed lost. Every write is
read back. The ledger executor holds the private-app token; agents never see it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from sindarin.connect import ConnectorError, call

HUBSPOT_API = "https://api.hubapi.com"
STAGES = {"open": "decisionmakerboughtin", "won": "closedwon", "lost": "closedlost"}
DEAL_TO_COMPANY = 5  # HubSpot-defined association type: deal → company


class HubSpot:
    name = "HubSpot"

    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None) -> None:
        if not token:
            raise ConnectorError("HubSpot: no token")
        self._http = httpx.Client(
            base_url=HUBSPOT_API,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20.0,
            transport=transport,
        )
        self._portal: str | None = None

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        return call(self._http, method, path, who="HubSpot", **kw)

    def portal_id(self) -> str:
        if self._portal is None:
            info = self._call("GET", "/account-info/v3/details")
            self._portal = str(info.get("portalId") or "")
        return self._portal

    def deal_url(self, deal_id: str) -> str:
        portal = self.portal_id()
        return f"https://app.hubspot.com/contacts/{portal}/record/0-3/{deal_id}" if portal else ""

    def company(self, name: str) -> str:
        """The company by exact name, created when missing."""
        found = self._call(
            "POST",
            "/crm/v3/objects/companies/search",
            json={
                "filterGroups": [
                    {"filters": [{"propertyName": "name", "operator": "EQ", "value": name}]}
                ],
                "limit": 1,
            },
        )
        rows = found.get("results") or []
        if rows:
            return str(rows[0]["id"])
        made = self._call("POST", "/crm/v3/objects/companies", json={"properties": {"name": name}})
        return str(made["id"])

    def _find_deal(self, dealname: str) -> str | None:
        found = self._call(
            "POST",
            "/crm/v3/objects/deals/search",
            json={
                "filterGroups": [
                    {"filters": [{"propertyName": "dealname", "operator": "EQ", "value": dealname}]}
                ],
                "limit": 1,
            },
        )
        rows = found.get("results") or []
        return str(rows[0]["id"]) if rows else None

    def upsert_deal(
        self,
        deal_id: str,
        *,
        account: str,
        product: str,
        amount_cents: int,
        stage: str,
        known_id: str | None = None,
    ) -> dict[str, Any]:
        """Create the deal at its stage or move it there; read it back before reporting."""
        if stage not in STAGES:
            raise ConnectorError(f"HubSpot: unknown stage {stage}")
        dealname = f"{deal_id} · {account} · {product}"
        props = {"dealstage": STAGES[stage], "amount": f"{amount_cents / 100:.2f}"}
        hubspot_id = known_id or self._find_deal(dealname)
        if hubspot_id:
            self._call("PATCH", f"/crm/v3/objects/deals/{hubspot_id}", json={"properties": props})
        else:
            company = self.company(account)
            made = self._call(
                "POST",
                "/crm/v3/objects/deals",
                json={
                    "properties": {
                        "dealname": dealname,
                        "pipeline": "default",
                        "closedate": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        **props,
                    },
                    "associations": [
                        {
                            "to": {"id": company},
                            "types": [
                                {
                                    "associationCategory": "HUBSPOT_DEFINED",
                                    "associationTypeId": DEAL_TO_COMPANY,
                                }
                            ],
                        }
                    ],
                },
            )
            hubspot_id = str(made["id"])
        seen = self._call(
            "GET", f"/crm/v3/objects/deals/{hubspot_id}", params={"properties": "dealstage,amount"}
        )
        got = (seen.get("properties") or {}).get("dealstage")
        if got != STAGES[stage]:
            raise ConnectorError(f"HubSpot: deal {hubspot_id} reads {got}, not {STAGES[stage]}")
        return {
            "crm_deal_id": hubspot_id,
            "crm_stage": STAGES[stage],
            "crm_url": self.deal_url(hubspot_id),
        }
