"""Entry point: uv run python -m sio_events"""

from .service import EventsService

if __name__ == "__main__":
    EventsService().run()
