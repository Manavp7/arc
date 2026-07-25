"""Entry point: uv run python -m sio_spatial"""

from .service import SpatialService

if __name__ == "__main__":
    SpatialService().run()
