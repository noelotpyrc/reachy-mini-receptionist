"""Motion CLI tools for Reachy Mini — uses REST API."""

import time

import click

from reachy_mini_brain import robot


@click.group()
def cli():
    pass


@cli.command()
def wake_up():
    """Wake up the robot."""
    robot.wake_up()
    click.echo("Robot is awake")


@cli.command()
def sleep():
    """Put the robot to sleep."""
    robot.go_to_sleep()
    click.echo("Robot is asleep")


@cli.command()
@click.option("--pitch", default=0.0, help="Pitch in degrees (pos=down, neg=up)")
@click.option("--roll", default=0.0, help="Roll in degrees (tilt)")
@click.option("--yaw", default=0.0, help="Yaw in degrees (pos=left, neg=right)")
@click.option("--duration", default=1.0, help="Movement duration in seconds")
def move_head(pitch, roll, yaw, duration):
    """Move the head to a given orientation (degrees)."""
    robot.goto(pitch=pitch, roll=roll, yaw=yaw, duration=duration)
    click.echo(f"Head moved to pitch={pitch} roll={roll} yaw={yaw}")


@cli.command()
@click.option("--angle", required=True, type=float, help="Angle in degrees")
@click.option("--duration", default=1.0, help="Movement duration in seconds")
def rotate_body(angle, duration):
    """Rotate the body to a given angle (degrees)."""
    robot.goto(body_yaw=angle, duration=duration)
    click.echo(f"Body rotated to {angle} degrees")


@cli.command()
@click.option("--left", default=0.0, help="Left antenna angle in degrees (pos=up)")
@click.option("--right", default=0.0, help="Right antenna angle in degrees (pos=up)")
def antennas(left, right):
    """Set antenna positions (degrees). Positive = up."""
    robot.set_target(antennas=(left, right))
    click.echo(f"Antennas set to left={left} right={right}")


@cli.command()
def nod():
    """Nod the head (yes gesture)."""
    for _ in range(2):
        robot.goto(pitch=15, duration=0.3)
        robot.goto(pitch=0, duration=0.3)
    click.echo("Nodded")


@cli.command()
def shake():
    """Shake the head (no gesture)."""
    robot.goto(yaw=20, duration=0.3)
    robot.goto(yaw=-20, duration=0.3)
    robot.goto(yaw=20, duration=0.3)
    robot.goto(yaw=0, duration=0.3)
    click.echo("Shook head")


@cli.command()
@click.option(
    "--direction",
    type=click.Choice(["left", "right", "up", "down", "center"]),
    required=True,
)
def look(direction):
    """Look in a preset direction."""
    presets = {
        "left": dict(yaw=30),
        "right": dict(yaw=-30),
        "up": dict(pitch=-20),
        "down": dict(pitch=20),
        "center": dict(),
    }
    robot.goto(**presets[direction], duration=0.8)
    click.echo(f"Looking {direction}")


if __name__ == "__main__":
    cli()
