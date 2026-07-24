"""Entry point: uv run python -m sio_tracking"""

from .service import TrackingService

if __name__ == "__main__":
    TrackingService().run()
