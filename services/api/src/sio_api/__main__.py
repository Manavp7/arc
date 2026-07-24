"""Entry point: uv run python -m sio_api"""

from .app import ApiService

if __name__ == "__main__":
    ApiService().run()
