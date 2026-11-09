from app.providers.base import Provider


def create_sender(context):
    from app.providers.whatsapp.sender import WhatsAppSender
    return WhatsAppSender(api_token=context.api_token)


provider = Provider(
    id="whatsapp", label="Whapi.Cloud", create_sender=create_sender,
    supports_media=True, supports_groups=True,
)
