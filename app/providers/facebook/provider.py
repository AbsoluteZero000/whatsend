from app.providers.base import Provider


def create_sender(context):
    from .sender import FacebookPageSender
    return FacebookPageSender(context.api_token)


# Connections are created by "Connect Facebook" (OAuth), so the token form hides this provider.
provider = Provider(
    id="facebook", label="Facebook Page", create_sender=create_sender,
    supports_media=True, supports_groups=True, auth="managed",
)
