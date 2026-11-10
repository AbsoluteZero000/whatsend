from app.providers.base import Provider


def create_sender(context):
    from .sender import InstagramSender
    return InstagramSender(context.api_token)


# Creator and Business accounts arrive through "Connect Instagram" (Instagram Login).
provider = Provider(
    id="instagram", label="Instagram", create_sender=create_sender,
    supports_media=True, supports_groups=True, auth="managed",
)
