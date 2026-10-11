"""A durable, project-local MCP development harness."""

__version__ = "0.1.1"


def main() -> None:
    from .cli import main as cli_main

    cli_main()
