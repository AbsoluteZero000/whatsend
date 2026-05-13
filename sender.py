# sender.py — Whapi.Cloud version
import requests
from pathlib import Path

_BASE_URL = "https://gate.whapi.cloud"

class WhatsAppSender:
    def __init__(self, api_token: str, timeout: int = 30):
        self.token = api_token
        self.timeout = timeout
        self.headers = {"accept": "application/json", "authorization": f"Bearer {api_token}"}

    def __send_text(self, chat_id: str, message: str) -> dict:
        response = requests.post(
            f"{_BASE_URL}/messages/text",
            headers=self.headers,
            json={"to": chat_id, "body": message},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def __send_image(self, chat_id: str, image_path: str, caption: str = "") -> dict:
        p = Path(image_path)
        with p.open("rb") as fh:
            response = requests.post(
                f"{_BASE_URL}/messages/image",
                headers={"authorization": f"Bearer {self.token}"},
                data={"to": chat_id, "caption": caption},
                files={"media": (p.name, fh, "image/jpeg")},
                timeout=self.timeout,
            )
        response.raise_for_status()
        return response.json()

    def send(self, chat_id: str, message: str, image_path: str | None = None) -> dict:
        if image_path:
            return self.__send_image(chat_id, image_path, caption=message)
        return self.__send_text(chat_id, message)
