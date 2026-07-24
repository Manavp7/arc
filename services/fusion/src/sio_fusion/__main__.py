"""Entry point: uv run python -m sio_fusion"""

from .service import FusionService

if __name__ == "__main__":
    FusionService().run()
