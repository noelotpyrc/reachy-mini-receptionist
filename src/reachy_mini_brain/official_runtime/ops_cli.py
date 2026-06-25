"""Developer CLI for official-runtime operations."""

from __future__ import annotations

import json
from typing import Any

import click

from . import ops_core
from .ops_core import ActionResult, OpsConfig, OpsError


class OpsGroup(click.Group):
    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except OpsError as exc:
            raise click.ClickException(str(exc)) from exc


@click.group(cls=OpsGroup)
@click.option(
    "--confirm-physical",
    is_flag=True,
    help="Authorize actions that can move the robot, use media, or start live robot output.",
)
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
@click.pass_context
def cli(ctx: click.Context, confirm_physical: bool, json_output: bool) -> None:
    """Manage backend, robot, and runner ops for the official runtime."""

    ctx.obj = {
        "config": OpsConfig.from_env(),
        "confirm_physical": confirm_physical,
        "json_output": json_output,
    }


@cli.command("status")
@click.option("--include-robot", is_flag=True, help="Also query robot daemon read-only status.")
@click.pass_context
def status_cmd(ctx: click.Context, include_robot: bool) -> None:
    """Print aggregate Backend/Runner status and optionally Robot status."""

    config = _config(ctx)
    _emit(ctx, ops_core.aggregate_status(config, include_robot=include_robot))


@cli.group()
def backend() -> None:
    """Manage the persistent m1max S2S backend."""


@backend.command("status")
@click.pass_context
def backend_status_cmd(ctx: click.Context) -> None:
    _emit(ctx, ops_core.backend_status(_config(ctx)))


@backend.command("start")
@click.pass_context
def backend_start_cmd(ctx: click.Context) -> None:
    _emit(ctx, ops_core.start_backend(_config(ctx)))


@backend.command("stop")
@click.pass_context
def backend_stop_cmd(ctx: click.Context) -> None:
    _emit(ctx, ops_core.stop_backend(_config(ctx)))


@backend.command("restart")
@click.pass_context
def backend_restart_cmd(ctx: click.Context) -> None:
    _emit_many(ctx, ops_core.restart_backend(_config(ctx)))


@cli.group()
def runner() -> None:
    """Manage the per-run official-runtime process."""


@runner.command("status")
@click.pass_context
def runner_status_cmd(ctx: click.Context) -> None:
    _emit(ctx, ops_core.runner_status(_config(ctx)))


@runner.command("start")
@click.option("--record-audio/--no-record-audio", default=None, help="Persist raw/input/output audio artifacts for this run.")
@click.option("--record-video/--no-record-video", default=None, help="Persist raw video MKV artifacts for this run.")
@click.option("--capture-vision/--no-capture-vision", default=None, help="Persist per-frame vision capture JSONL for this run.")
@click.pass_context
def runner_start_cmd(
    ctx: click.Context,
    record_audio: bool | None,
    record_video: bool | None,
    capture_vision: bool | None,
) -> None:
    _emit(
        ctx,
        ops_core.start_runner(
            _config(ctx),
            authorized=_authorized(ctx),
            record_audio=record_audio,
            record_video=record_video,
            capture_vision=capture_vision,
        ),
    )


@runner.command("stop")
@click.option(
    "--include-unmanaged",
    is_flag=True,
    help="Also stop matching live-runner processes that were not launched by OPS.",
)
@click.pass_context
def runner_stop_cmd(ctx: click.Context, include_unmanaged: bool) -> None:
    _emit(
        ctx,
        ops_core.stop_runner(
            _config(ctx),
            authorized=_authorized(ctx),
            include_unmanaged=include_unmanaged,
        ),
    )


@cli.command("wake-robot")
@click.pass_context
def wake_robot_cmd(ctx: click.Context) -> None:
    """Wake robot, acquire media, and enable motors."""

    _emit(ctx, ops_core.wake_robot(_config(ctx), authorized=_authorized(ctx)))


@cli.command("sleep-robot")
@click.pass_context
def sleep_robot_cmd(ctx: click.Context) -> None:
    """Release media, sleep robot, and disable motors."""

    _emit(ctx, ops_core.sleep_robot(_config(ctx), authorized=_authorized(ctx)))


@cli.command("start-session")
@click.option("--record-audio/--no-record-audio", default=None, help="Persist raw/input/output audio artifacts for this run.")
@click.option("--record-video/--no-record-video", default=None, help="Persist raw video MKV artifacts for this run.")
@click.option("--capture-vision/--no-capture-vision", default=None, help="Persist per-frame vision capture JSONL for this run.")
@click.pass_context
def start_session_cmd(
    ctx: click.Context,
    record_audio: bool | None,
    record_video: bool | None,
    capture_vision: bool | None,
) -> None:
    """Ensure backend/robot are ready and start one live runner."""

    _emit_many(
        ctx,
        ops_core.start_session_with_options(
            _config(ctx),
            authorized=_authorized(ctx),
            record_audio=record_audio,
            record_video=record_video,
            capture_vision=capture_vision,
        ),
    )


@cli.command("stop-session")
@click.pass_context
def stop_session_cmd(ctx: click.Context) -> None:
    """Stop the live runner and tear down robot state."""

    _emit_many(ctx, ops_core.stop_session(_config(ctx), authorized=_authorized(ctx)))


@cli.command("shutdown")
@click.pass_context
def shutdown_cmd(ctx: click.Context) -> None:
    """Stop runner, sleep robot, and disable motors."""

    _emit_many(ctx, ops_core.shutdown(_config(ctx), authorized=_authorized(ctx)))


@cli.command("latest-run")
@click.pass_context
def latest_run_cmd(ctx: click.Context) -> None:
    """Print the latest-run pointer used by diagnosis tooling."""

    latest = ops_core.load_latest_run(_config(ctx))
    result = ActionResult(action="latest-run", status="ok" if latest else "missing", data={"latest_run": latest})
    _emit(ctx, result)


@cli.group(invoke_without_command=True)
@click.pass_context
def preflight(ctx: click.Context) -> None:
    """Run the full preflight or one exposed substep."""

    if ctx.invoked_subcommand is None:
        _emit_many(ctx, ops_core.full_preflight(_config(ctx), authorized=_authorized(ctx)))


@preflight.command("backend-health")
@click.pass_context
def preflight_backend_health_cmd(ctx: click.Context) -> None:
    _emit(ctx, ops_core.preflight_backend_health(_config(ctx)))


@preflight.command("robot-state")
@click.pass_context
def preflight_robot_state_cmd(ctx: click.Context) -> None:
    _emit(ctx, ops_core.preflight_robot_state(_config(ctx)))


@preflight.command("audio-playback")
@click.pass_context
def preflight_audio_playback_cmd(ctx: click.Context) -> None:
    _emit(ctx, ops_core.preflight_audio_playback(_config(ctx), authorized=_authorized(ctx)))


@preflight.command("policy-goodbye")
@click.pass_context
def preflight_policy_goodbye_cmd(ctx: click.Context) -> None:
    _emit(ctx, ops_core.preflight_policy(_config(ctx), authorized=_authorized(ctx), flow="goodbye"))


@preflight.command("policy-greet")
@click.pass_context
def preflight_policy_greet_cmd(ctx: click.Context) -> None:
    _emit(ctx, ops_core.preflight_policy(_config(ctx), authorized=_authorized(ctx), flow="greet"))


@preflight.command("policy-flow")
@click.pass_context
def preflight_policy_flow_cmd(ctx: click.Context) -> None:
    _emit(ctx, ops_core.preflight_policy(_config(ctx), authorized=_authorized(ctx), flow="goodbye-greet"))


def _config(ctx: click.Context) -> OpsConfig:
    return ctx.obj["config"]


def _authorized(ctx: click.Context) -> bool:
    return bool(ctx.obj["confirm_physical"])


def _emit_many(ctx: click.Context, results: list[ActionResult]) -> None:
    if ctx.obj["json_output"]:
        click.echo(json.dumps([result.to_dict() for result in results], indent=2))
    else:
        for result in results:
            _emit_text(result)
    _raise_if_failed(results)


def _emit(ctx: click.Context, result: ActionResult) -> None:
    if ctx.obj["json_output"]:
        click.echo(json.dumps(result.to_dict(), indent=2))
    else:
        _emit_text(result)
    _raise_if_failed([result])


def _emit_text(result: ActionResult) -> None:
    click.echo(f"{result.action}: {result.status}")
    if result.authorization_required:
        click.echo(f"  safety: {result.safety}; authorized={result.authorized}")
    if result.changed:
        click.echo("  changed: true")
    if result.human_quality_gate is not None:
        gate = result.human_quality_gate
        prefix = "required" if gate.required else "recommended"
        click.echo(f"  human quality gate ({prefix}): {gate.prompt}")
    for verification in result.machine_verification:
        detail = _compact(verification.details)
        suffix = f" {detail}" if detail else ""
        click.echo(f"  verify {verification.kind}: {verification.status}{suffix}")
    for error in result.errors:
        click.echo(f"  error: {error}", err=True)
    if result.data:
        for key, value in result.data.items():
            if key in {"backend", "runner", "robot"}:
                continue
            click.echo(f"  {key}: {_compact(value)}")


def _raise_if_failed(results: list[ActionResult]) -> None:
    failed = [result for result in results if result.status in {"failed", "degraded"}]
    if failed:
        raise click.ClickException("; ".join(f"{result.action}: {result.status}" for result in failed))


def _compact(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str, sort_keys=True)
    return str(value)


def main() -> None:
    try:
        cli()
    except OpsError as exc:
        raise click.ClickException(str(exc)) from exc


if __name__ == "__main__":
    main()
