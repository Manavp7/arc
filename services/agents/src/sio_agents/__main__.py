"""Entry point: uv run python -m sio_agents"""

from .service import AgentsService

if __name__ == "__main__":
    AgentsService().run()
