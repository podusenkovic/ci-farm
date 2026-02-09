"""Build execution and project synchronization."""

import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from rich.console import Console

from .config import Config, ProjectConfig, SlaveConfig
from .slave import SlaveConnection, SlaveBusyError

BUILD_MARKERS = {
    "Makefile": "make",
    "CMakeLists.txt": "cmake -B build && cmake --build build",
    "package.json": "npm install && npm run build",
    "Cargo.toml": "cargo build --release",
    "go.mod": "go build ./...",
    "pyproject.toml": "pip install -e . && python -m pytest",
    "setup.py": "pip install -e . && python -m pytest",
    ".ci/build.sh": "bash .ci/build.sh",
    "build.sh": "bash build.sh",
}


class BuildError(Exception):
    """Build failed."""


def detect_build_command(project_path: Path) -> Optional[str]:
    """Auto-detect build command based on project files."""
    for marker, command in BUILD_MARKERS.items():
        if (project_path / marker).exists():
            return command
    return None


def sync_project(
    project_path: Path,
    slave: SlaveConfig,
    exclude: list[str],
    console: Console,
    dry_run: bool = False,
) -> str:
    """Sync project to slave using rsync."""
    project_name = project_path.name
    remote_path = f"{slave.build_dir}/{project_name}"

    exclude_args = []
    for pattern in exclude:
        exclude_args.extend(["--exclude", pattern])

    ssh_cmd = f"ssh -p {slave.port}"
    if slave.key:
        ssh_cmd += f" -i {slave.key}"

    rsync_cmd = [
        "rsync",
        "-avz",
        "--delete",
        "-e", ssh_cmd,
        *exclude_args,
        f"{project_path}/",
        f"{slave.user}@{slave.host}:{remote_path}/",
    ]

    if dry_run:
        rsync_cmd.insert(1, "--dry-run")

    console.print(f"[dim]Syncing to {slave.name}:{remote_path}[/dim]")

    process = subprocess.Popen(
        rsync_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    for line in process.stdout:
        line = line.rstrip()
        if line:
            console.print(f"[dim]{line}[/dim]")

    process.wait()

    if process.returncode != 0:
        raise BuildError(f"rsync failed with code {process.returncode}")

    return remote_path


def run_build(
    conn: SlaveConnection,
    remote_path: str,
    command: str,
    timeout: int,
    console: Console,
) -> int:
    """Execute build command on slave."""
    console.print(f"\n[bold blue]Running:[/bold blue] {command}\n")

    def on_stdout(line: str):
        console.print(line)

    def on_stderr(line: str):
        console.print(f"[red]{line}[/red]")

    return conn.exec_command(
        command,
        working_dir=remote_path,
        timeout=timeout,
        on_stdout=on_stdout,
        on_stderr=on_stderr,
    )


def execute_build(
    project_path: Path,
    config: Config,
    slave_name: Optional[str] = None,
    build_command: Optional[str] = None,
    console: Optional[Console] = None,
) -> int:
    """Execute full build pipeline: sync -> build -> report."""
    if console is None:
        console = Console()

    slave = config.get_slave(slave_name)
    if not slave:
        if slave_name:
            console.print(f"[red]Slave '{slave_name}' not found[/red]")
        else:
            console.print("[red]No slaves configured[/red]")
        return 1

    command = build_command or config.project.build_command
    if not command:
        command = detect_build_command(project_path)

    if not command:
        console.print("[red]Could not detect build command. Specify it in config or CLI.[/red]")
        return 1

    console.print(f"[bold green]Building on:[/bold green] {slave.name} ({slave.host})")

    try:
        with SlaveConnection(slave) as conn:
            if conn.is_busy():
                lock_info = conn.get_lock_info()
                if lock_info:
                    project, _ = lock_info
                    console.print(f"[red]Slave is busy with '{project}'[/red]")
                else:
                    console.print("[red]Slave is busy[/red]")
                return 1

            project_name = project_path.name
            conn.acquire_lock(project_name)

            try:
                for pre_cmd in config.project.pre_sync:
                    console.print(f"[dim]Pre-sync: {pre_cmd}[/dim]")
                    subprocess.run(pre_cmd, shell=True, cwd=project_path, check=True)

                remote_path = sync_project(
                    project_path,
                    slave,
                    config.project.exclude,
                    console,
                )

                exit_code = run_build(
                    conn,
                    remote_path,
                    command,
                    config.project.timeout,
                    console,
                )

                if exit_code == 0:
                    for post_cmd in config.project.post_build:
                        console.print(f"[dim]Post-build: {post_cmd}[/dim]")
                        conn.exec_command(
                            post_cmd,
                            working_dir=remote_path,
                            on_stdout=lambda l: console.print(f"[dim]{l}[/dim]"),
                            on_stderr=lambda l: console.print(f"[red]{l}[/red]"),
                        )

                console.print()
                if exit_code == 0:
                    console.print("[bold green]Build successful![/bold green]")
                else:
                    console.print(f"[bold red]Build failed with code {exit_code}[/bold red]")

                return exit_code

            finally:
                conn.release_lock()

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        return 1
