"""Entry point: uv run python -m sio_worldmodel"""

from .service import WorldModelService

if __name__ == "__main__":
    WorldModelService().run()
