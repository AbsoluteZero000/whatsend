from app.providers.base import Provider


def create_sender(context):
    from app.providers.native.sender import NativeSender
    if not context.session_id:
        raise ValueError("A WhatsApp connection is required")
    return NativeSender(session_id=context.session_id)


provider = Provider(
    id="native", label="WhatsApp QR", create_sender=create_sender,
    supports_media=True, supports_groups=True, auth="managed", delivery="whatsapp_worker",
)
