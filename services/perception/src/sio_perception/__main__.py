"""Entry point: uv run python -m sio_perception"""

from .service import PerceptionService

if __name__ == "__main__":
    PerceptionService().run()
