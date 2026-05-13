import httpx
from pathlib import Path


class WhatsAppSender:
    def __init__(self, api_token: str, timeout: int = 30, base_url: str = "https://gate.whapi.cloud"):
        self.token = api_token
        self.timeout = timeout
        self.base_url = base_url
        self.headers = {"accept": "application/json", "authorization": f"Bearer {api_token}"}

    async def send_text(self, chat_id: str, message: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/messages/text",
                headers=self.headers,
                json={"to": chat_id, "body": message},
            )
        response.raise_for_status()
        return response.json()

    async def send_image(self, chat_id: str, image_path: str, caption: str = "") -> dict:
        p = Path(image_path)
        with p.open("rb") as fh:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/messages/image",
                    headers={"authorization": f"Bearer {self.token}"},
                    data={"to": chat_id, "caption": caption},
                    files={"media": (p.name, fh, "image/jpeg")},
                )
        response.raise_for_status()
        return response.json()

    async def get_groups(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{self.base_url}/groups",
                headers=self.headers,
                params={"count": 100},
            )
        response.raise_for_status()
        data = response.json()
        return data.get("groups", [])

    async def send(self, chat_id: str, message: str, image_path: str | None = None) -> dict:
        if image_path:
            return await self.send_image(chat_id, image_path, caption=message)
        return await self.send_text(chat_id, message)
