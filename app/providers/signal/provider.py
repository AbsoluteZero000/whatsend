from app.providers import Provider
from .sender import SignalSender, validate_credentials

provider = Provider(
    id="signal", label="Signal", create_sender=lambda context: SignalSender(context.api_token),
    supports_media=True, supports_groups=True, credential_label="Signal connection token",
    validate_credentials=validate_credentials,
    credential_help="An administrator links your Signal account to the private service and issues its connection token. Paste that token here. Recipients can be phone numbers or group IDs from the picker.",
)
