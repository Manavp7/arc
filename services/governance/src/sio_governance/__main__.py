"""Entry point: uv run python -m sio_governance"""

from .service import GovernanceService

if __name__ == "__main__":
    GovernanceService().run()
