from app.providers import Provider
from .client import TikTokPublisher, validate_token

provider = Provider(
    id="tiktok", label="TikTok", kind="publishing",
    create_sender=lambda context: TikTokPublisher(context.api_token),
    credential_label="TikTok user access token", validate_credentials=validate_token,
    credential_help="Use a user access token authorized for video.publish by your TikTok developer app. After saving, use Publish posts to select content, visibility and consent. Unaudited apps can publish privately only. Renew expired tokens using your developer app's authorization flow.",
)
