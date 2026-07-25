"""Entry point: uv run python -m sio_analytics"""

from .service import AnalyticsService

if __name__ == "__main__":
    AnalyticsService().run()
