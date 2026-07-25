"""Entry point: uv run python -m sio_missions"""

from .service import MissionsService

if __name__ == "__main__":
    MissionsService().run()
