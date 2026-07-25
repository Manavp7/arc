"""Entry point: uv run python -m sio_copilot"""

from .service import CopilotService

if __name__ == "__main__":
    CopilotService().run()
