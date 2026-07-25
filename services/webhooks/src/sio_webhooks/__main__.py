"""Entry point: uv run python -m sio_webhooks"""

from .service import WebhooksService

if __name__ == "__main__":
    WebhooksService().run()
