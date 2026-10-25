from pathlib import Path

import requests


class MaxApiError(RuntimeError):
    pass


class MaxClient:
    def __init__(self, token: str, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": token})

    def _request(self, method: str, path: str, **kwargs) -> dict:
        response = self.session.request(method, f"{self.base_url}{path}", timeout=100, **kwargs)
        if not response.ok:
            raise MaxApiError(f"MAX API {response.status_code}: {response.text[:300]}")
        return response.json()

    def get_updates(self, marker: int | None, timeout: int = 30) -> dict:
        params = {
            "timeout": timeout,
            "limit": 100,
            "types": "bot_started,message_created,message_callback",
        }
        if marker is not None:
            params["marker"] = marker
        return self._request("GET", "/updates", params=params)

    @staticmethod
    def keyboard(buttons: list[list[dict]]) -> dict:
        return {"type": "inline_keyboard", "payload": {"buttons": buttons}}

    def send_message(
        self,
        user_id: int,
        text: str,
        buttons: list[list[dict]] | None = None,
        image_token: str = "",
        notify: bool = True,
    ) -> dict:
        attachments = []
        if image_token:
            attachments.append({"type": "image", "payload": {"token": image_token}})
        if buttons:
            attachments.append(self.keyboard(buttons))
        body = {"text": text, "notify": notify}
        if attachments:
            body["attachments"] = attachments
        return self._request("POST", "/messages", params={"user_id": user_id}, json=body)

    def answer_callback(self, callback_id: str, text: str, buttons: list[list[dict]] | None = None) -> dict:
        message = {"text": text}
        if buttons:
            message["attachments"] = [self.keyboard(buttons)]
        return self._request("POST", "/answers", params={"callback_id": callback_id}, json={"message": message})

    def is_channel_member(self, channel_id: int, user_id: int) -> bool:
        data = self._request(
            "GET",
            f"/chats/{channel_id}/members",
            params={"user_ids": str(user_id)},
        )
        return any(int(member["user_id"]) == user_id for member in data.get("members", []))

    def upload_image(self, path: str) -> str:
        upload = self._request("POST", "/uploads", params={"type": "image"})
        with Path(path).open("rb") as file:
            response = requests.post(upload["url"], files={"data": file}, timeout=120)
        if not response.ok:
            raise MaxApiError(f"upload failed: {response.status_code} {response.text[:300]}")
        payload = response.json()
        token = payload.get("token") or upload.get("token")
        if not token:
            raise MaxApiError("upload response has no token")
        return token

