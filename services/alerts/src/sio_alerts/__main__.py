"""Entry point: uv run python -m sio_alerts"""

from .service import AlertsService

if __name__ == "__main__":
    AlertsService().run()
