"""Entry point: uv run python -m sio_workflow"""

from .service import WorkflowService

if __name__ == "__main__":
    WorkflowService().run()
