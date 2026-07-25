"""Entry point: uv run python -m sio_simulation"""

from .service import SimulationService

if __name__ == "__main__":
    SimulationService().run()
