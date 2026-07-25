"""Entry point: uv run python -m sio_prediction"""

from .service import PredictionService

if __name__ == "__main__":
    PredictionService().run()
