"""State CLI tools for Reachy Mini — uses REST API."""

import json

import click

from reachy_mini_brain import robot


@click.group()
def cli():
    pass


@cli.command()
def get_state():
    """Print full robot state as JSON."""
    state = robot.get_state()
    click.echo(json.dumps(state, indent=2))


if __name__ == "__main__":
    cli()
