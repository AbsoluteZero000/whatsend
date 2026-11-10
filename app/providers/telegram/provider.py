from app.providers import Provider
from .sender import TelegramSender, validate_token

provider = Provider(
    id="telegram", label="Telegram", create_sender=lambda context: TelegramSender(context.api_token),
    supports_media=True, credential_label="Telegram bot token", validate_credentials=validate_token,
    credential_help="Create a bot with @BotFather and paste its token. Use a numeric chat ID or @channel username as the recipient. Users must start the bot first; add it to groups or channels with permission to post.",
)
