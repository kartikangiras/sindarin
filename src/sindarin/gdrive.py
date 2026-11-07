"""Google Drive and Slides: the month's board pack where the board already looks.

When a period locks (or on `pack <period>`), the Switch executor puts the report, its charts
and the exports into a Drive folder for the month and builds a Slides deck from them, then
posts both links in the room. The service account the Sheets write-back already uses is the
credential; the folder it writes into is one the team shares with it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from sindarin.connect import ConnectorError, call

DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
SLIDES = "https://slides.googleapis.com/v1"
FOLDER = "application/vnd.google-apps.folder"
PRESENTATION = "application/vnd.google-apps.presentation"
SCOPES = ("https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/presentations")


@dataclass(frozen=True)
class Slide:
    title: str
    body: str = ""  # plain lines, one per row
    image_url: str = ""  # a Drive image the deck can fetch


def _credentials(sa_path: str = "") -> Any:
    """Google credentials: a service-account key when one is given, else Application Default
    Credentials, which `gcloud auth application-default login` writes for your own account.
    The second path needs no key file, so it works where an organisation policy forbids
    service-account keys, and the files land in your own Drive."""
    if sa_path:
        from google.oauth2.service_account import Credentials

        return Credentials.from_service_account_file(sa_path, scopes=list(SCOPES))
    import google.auth

    try:
        creds, _ = google.auth.default(scopes=list(SCOPES))
    except Exception as exc:  # DefaultCredentialsError and friends
        raise ConnectorError(
            "Google Drive: no credentials — set SINDARIN_GOOGLE_SA to a key file, or run "
            "`gcloud auth application-default login --scopes=https://www.googleapis.com/auth/"
            "drive,https://www.googleapis.com/auth/presentations,https://www.googleapis.com/"
            f"auth/cloud-platform` ({exc.__class__.__name__})"
        ) from None
    return creds


def google_token(sa_path: str = "") -> tuple[str, str]:
    """A fresh access token and the quota project the request must name, if any."""
    from google.auth.transport.requests import Request

    creds = _credentials(sa_path)
    creds.refresh(Request())
    return str(creds.token), str(getattr(creds, "quota_project_id", "") or "")


def service_account_token(path: str) -> str:
    return google_token(path)[0]


class GoogleDocs:
    name = "Google Drive"

    def __init__(
        self,
        folder_id: str,
        *,
        sa_path: str = "",
        token: Callable[[], str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not folder_id:
            raise ConnectorError("Google Drive: no folder id")
        self.folder_id = folder_id
        self._quota_project = ""
        if token is None:
            _credentials(sa_path)  # fail now, with a readable reason, not at the first pack
            self._token = lambda: self._fresh(sa_path)
        else:
            self._token = token
        self._http = httpx.Client(timeout=60.0, transport=transport)

    def _fresh(self, sa_path: str) -> str:
        token, project = google_token(sa_path)
        self._quota_project = project
        return token

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, url: str, **kw: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token()}", **(kw.pop("headers", None) or {})}
        if self._quota_project:
            headers["x-goog-user-project"] = self._quota_project
        return call(self._http, method, url, who="Google", headers=headers, **kw)

    def whoami(self) -> str:
        """The account the connector acts as, read from Drive."""
        about = self._call("GET", f"{DRIVE}/about", params={"fields": "user(emailAddress)"})
        return str((about.get("user") or {}).get("emailAddress") or "")

    # -- Drive --------------------------------------------------------------------------------

    def folder(self, name: str) -> str:
        """The month's folder under the shared one, created when missing."""
        q = (
            f"name = '{name}' and mimeType = '{FOLDER}' and '{self.folder_id}' in parents "
            "and trashed = false"
        )
        found = self._call(
            "GET",
            f"{DRIVE}/files",
            params={
                "q": q,
                "fields": "files(id,name)",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            },
        )
        rows = found.get("files") or []
        if rows:
            return str(rows[0]["id"])
        made = self._call(
            "POST",
            f"{DRIVE}/files",
            params={"supportsAllDrives": "true", "fields": "id"},
            json={"name": name, "mimeType": FOLDER, "parents": [self.folder_id]},
        )
        return str(made["id"])

    def upload(self, folder_id: str, name: str, data: bytes, mime: str) -> dict[str, str]:
        """A file into the folder, replacing one of the same name; the web link back."""
        q = f"name = '{name}' and '{folder_id}' in parents and trashed = false"
        found = self._call(
            "GET",
            f"{DRIVE}/files",
            params={
                "q": q,
                "fields": "files(id)",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            },
        )
        existing = (found.get("files") or [{}])[0].get("id")
        boundary = "sindarin-pack"
        meta = {"name": name, "mimeType": mime}
        if not existing:
            meta["parents"] = [folder_id]
        body = (
            (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps(meta)}\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n"
            ).encode()
            + data
            + f"\r\n--{boundary}--".encode()
        )
        url = f"{UPLOAD}/files/{existing}" if existing else f"{UPLOAD}/files"
        made = self._call(
            "PATCH" if existing else "POST",
            url,
            params={
                "uploadType": "multipart",
                "supportsAllDrives": "true",
                "fields": "id,webViewLink",
            },
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
            content=body,
        )
        return {"id": str(made["id"]), "link": str(made.get("webViewLink") or "")}

    def share_link(self, file_id: str) -> str:
        """Anyone with the link may view: what Slides needs to pull an image in."""
        self._call(
            "POST",
            f"{DRIVE}/files/{file_id}/permissions",
            params={"supportsAllDrives": "true"},
            json={"role": "reader", "type": "anyone"},
        )
        return f"https://drive.google.com/uc?export=view&id={file_id}"

    # -- Slides -------------------------------------------------------------------------------

    def deck(self, folder_id: str, title: str, slides: Sequence[Slide]) -> dict[str, str]:
        """A presentation in the folder from the slides given; the first is the title slide."""
        q = (
            f"name = '{title}' and mimeType = '{PRESENTATION}' and '{folder_id}' in parents "
            "and trashed = false"
        )
        found = self._call(
            "GET",
            f"{DRIVE}/files",
            params={
                "q": q,
                "fields": "files(id)",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            },
        )
        rows = found.get("files") or []
        if rows:
            pres_id = str(rows[0]["id"])
            current = self._call("GET", f"{SLIDES}/presentations/{pres_id}")
            wipe = [
                {"deleteObject": {"objectId": s["objectId"]}} for s in current.get("slides") or []
            ]
        else:
            made = self._call(
                "POST",
                f"{DRIVE}/files",
                params={"supportsAllDrives": "true", "fields": "id"},
                json={"name": title, "mimeType": PRESENTATION, "parents": [folder_id]},
            )
            pres_id = str(made["id"])
            current = self._call("GET", f"{SLIDES}/presentations/{pres_id}")
            wipe = [
                {"deleteObject": {"objectId": s["objectId"]}} for s in current.get("slides") or []
            ]
        requests: list[dict[str, Any]] = []
        for i, slide in enumerate(slides):
            sid = f"sindarin-slide-{i}"
            if i == 0:
                requests.append(
                    {
                        "createSlide": {
                            "objectId": sid,
                            "slideLayoutReference": {"predefinedLayout": "TITLE"},
                            "placeholderIdMappings": [
                                {
                                    "layoutPlaceholder": {"type": "CENTERED_TITLE"},
                                    "objectId": f"{sid}-t",
                                },
                                {"layoutPlaceholder": {"type": "SUBTITLE"}, "objectId": f"{sid}-s"},
                            ],
                        }
                    }
                )
                requests.append({"insertText": {"objectId": f"{sid}-t", "text": slide.title}})
                if slide.body:
                    requests.append({"insertText": {"objectId": f"{sid}-s", "text": slide.body}})
                continue
            requests.append(
                {
                    "createSlide": {
                        "objectId": sid,
                        "slideLayoutReference": {"predefinedLayout": "BLANK"},
                    }
                }
            )
            requests.append(
                {
                    "createShape": {
                        "objectId": f"{sid}-h",
                        "shapeType": "TEXT_BOX",
                        "elementProperties": {
                            "pageObjectId": sid,
                            "size": {
                                "width": {"magnitude": 620, "unit": "PT"},
                                "height": {"magnitude": 40, "unit": "PT"},
                            },
                            "transform": {
                                "scaleX": 1,
                                "scaleY": 1,
                                "translateX": 50,
                                "translateY": 20,
                                "unit": "PT",
                            },
                        },
                    }
                }
            )
            requests.append({"insertText": {"objectId": f"{sid}-h", "text": slide.title}})
            requests.append(
                {
                    "updateTextStyle": {
                        "objectId": f"{sid}-h",
                        "style": {"bold": True, "fontSize": {"magnitude": 20, "unit": "PT"}},
                        "fields": "bold,fontSize",
                    }
                }
            )
            if slide.image_url:
                requests.append(
                    {
                        "createImage": {
                            "objectId": f"{sid}-i",
                            "url": slide.image_url,
                            "elementProperties": {
                                "pageObjectId": sid,
                                "size": {
                                    "width": {"magnitude": 600, "unit": "PT"},
                                    "height": {"magnitude": 330, "unit": "PT"},
                                },
                                "transform": {
                                    "scaleX": 1,
                                    "scaleY": 1,
                                    "translateX": 60,
                                    "translateY": 70,
                                    "unit": "PT",
                                },
                            },
                        }
                    }
                )
            elif slide.body:
                requests.append(
                    {
                        "createShape": {
                            "objectId": f"{sid}-b",
                            "shapeType": "TEXT_BOX",
                            "elementProperties": {
                                "pageObjectId": sid,
                                "size": {
                                    "width": {"magnitude": 620, "unit": "PT"},
                                    "height": {"magnitude": 320, "unit": "PT"},
                                },
                                "transform": {
                                    "scaleX": 1,
                                    "scaleY": 1,
                                    "translateX": 50,
                                    "translateY": 70,
                                    "unit": "PT",
                                },
                            },
                        }
                    }
                )
                requests.append({"insertText": {"objectId": f"{sid}-b", "text": slide.body}})
        self._call(
            "POST",
            f"{SLIDES}/presentations/{pres_id}:batchUpdate",
            json={"requests": requests + wipe},
        )
        return {"id": pres_id, "link": f"https://docs.google.com/presentation/d/{pres_id}/edit"}

    # -- the board pack -----------------------------------------------------------------------

    def board_pack(
        self,
        period: str,
        label: str,
        *,
        report_lines: Sequence[str],
        charts: Sequence[tuple[str, bytes, str, str]],  # (caption, png, mime, filename)
        exports: Sequence[tuple[str, bytes, str]],  # (filename, data, mime)
        report_text: str = "",
    ) -> dict[str, Any]:
        folder = self.folder(f"Sindarin {period}")
        files = 0
        slides = [Slide(f"{label} board pack", "Sindarin · the month as the books hold it")]
        slides.append(Slide("The month", "\n".join(report_lines)))
        for caption, png, mime, filename in charts:
            up = self.upload(folder, filename, png, mime)
            files += 1
            slides.append(Slide(caption, image_url=self.share_link(up["id"])))
        if report_text:
            self.upload(folder, f"report-{period}.md", report_text.encode("utf-8"), "text/markdown")
            files += 1
        for filename, data, mime in exports:
            self.upload(folder, filename, data, mime)
            files += 1
        deck = self.deck(folder, f"{label} board pack", slides)
        return {
            "folder_id": folder,
            "folder_link": f"https://drive.google.com/drive/folders/{folder}",
            "deck_id": deck["id"],
            "deck_link": deck["link"],
            "files": files,
            "slides": len(slides),
        }
