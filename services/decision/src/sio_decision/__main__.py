"""Entry point: uv run python -m sio_decision"""

from .service import DecisionService

if __name__ == "__main__":
    DecisionService().run()
