"""Entry point: uv run python -m sio_ingest"""

from .service import IngestService

if __name__ == "__main__":
    IngestService().run()
