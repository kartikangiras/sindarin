"""Files into rooms through Switch (design v5, reporting rail): an attachment uploaded to the
Gateway's chat route, then a message that carries it, in the thread the command came from."""

from __future__ import annotations

import logging

from sindarin.switch.gateway import GatewayClient, GatewayError

log = logging.getLogger("sindarin.files")


class GatewayFiles:
    def __init__(self, gateway: GatewayClient) -> None:
        self.gateway = gateway
        self._joined: set[str] = set()

    def _join(self, room_id: str) -> None:
        """The Gateway user must be a member before it may post into a room; once per room."""
        if room_id not in self._joined:
            self.gateway.join_chat(room_id)
            self._joined.add(room_id)

    def post_files(
        self, room_id: str, text: str, files: list[tuple[str, bytes, str]], thread_id: str | None
    ) -> bool:
        """One message carrying several files (a report and its charts)."""
        try:
            self._join(room_id)
            uploads = [
                self.gateway.upload_attachment(room_id, name, data, mime)
                for name, data, mime in files
            ]
            self.gateway.send_message(
                room_id, text, upload_ids=uploads, thread_root_id=thread_id or None
            )
            return True
        except Exception as exc:
            log.warning("files not posted: %s", str(exc)[:160])
            return False

    def post_file(
        self, room_id: str, text: str, filename: str, data: bytes, mime: str, thread_id: str | None
    ) -> bool:
        try:
            self._join(room_id)
            upload = self.gateway.upload_attachment(room_id, filename, data, mime)
            self.gateway.send_message(
                room_id, text, upload_ids=[upload], thread_root_id=thread_id or None
            )
            return True
        except GatewayError as exc:
            log.warning("attachment not posted: %s", str(exc)[:160])
            return False
