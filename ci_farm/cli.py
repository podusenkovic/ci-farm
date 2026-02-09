"""Command-line interface for CI Farm."""

import argparse
import shlex
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table

from . import __version__
from .builder import detect_build_command, execute_build
from .config import Config, SlaveConfig, GLOBAL_CONFIG_PATH
from .slave import (
    DEFAULT_CHECK_TOOLS,
    SlaveConnection,
    SlaveConnectionError,
    check_slave_available,
    find_available_slave,
)


console = Console()

KNOWN_SUBCOMMANDS = frozenset({
    "build", "status", "add", "remove", "init", "config", "unlock",
})


def cmd_build(args: argparse.Namespace) -> int:
    """Execute build on a slave."""
    project_path = Path(args.path).resolve()

    if not project_path.exists():
        console.print(f"[red]Path does not exist: {project_path}[/red]")
        return 1

    config = Config.load(project_path)

    if not config.slaves:
        console.print("[red]No slaves configured. Run 'ci add' first.[/red]")
        return 1

    slave_name = args.on
    if not slave_name and args.auto:
        slave = find_available_slave(config.slaves)
        if slave:
            slave_name = slave.name
        else:
            console.print("[red]No available slaves found[/red]")
            return 1

    return execute_build(
        project_path,
        config,
        slave_name=slave_name,
        build_command=args.command,
        console=console,
    )


def cmd_status(args: argparse.Namespace) -> int:
    """Show status of all slaves."""
    config = Config.load()

    if not config.slaves:
        console.print("[yellow]No slaves configured. Run 'ci add' first.[/yellow]")
        return 0

    table = Table(title="CI Farm Slaves")
    table.add_column("Name", style="cyan")
    table.add_column("Host", style="dim")
    table.add_column("Status")
    table.add_column("Info", style="dim")

    for slave in config.slaves:
        available, info = check_slave_available(slave)
        if available:
            status = "[green]Available[/green]"
        else:
            status = "[red]Unavailable[/red]"
        table.add_row(slave.name, f"{slave.user}@{slave.host}:{slave.port}", status, info or "")

    console.print(table)
    return 0


def _print_tools_check(tools: list[tuple[str, Optional[str]]]) -> None:
    """Display tools availability check results."""
    table = Table(title="Tools Check")
    table.add_column("Tool", style="cyan")
    table.add_column("Status")
    table.add_column("Version", style="dim")

    for name, version in tools:
        if version is not None:
            table.add_row(name, "[green]installed[/green]", version)
        else:
            table.add_row(name, "[red]missing[/red]", "")

    console.print(table)


def cmd_add(args: argparse.Namespace) -> int:
    """Add a new slave to configuration."""
    config = Config.load()

    for slave in config.slaves:
        if slave.name == args.name:
            console.print(f"[red]Slave '{args.name}' already exists[/red]")
            return 1

    new_slave = SlaveConfig(
        name=args.name,
        host=args.host,
        user=args.user,
        port=args.port,
        key=args.key,
        build_dir=args.build_dir,
    )

    try:
        with SlaveConnection(new_slave) as conn:
            tools = conn.check_tools(DEFAULT_CHECK_TOOLS)
            _print_tools_check(tools)

            missing = [name for name, version in tools if version is None]
            if missing and not args.force:
                console.print(
                    "[yellow]Some tools are missing. "
                    "Use --force to add anyway[/yellow]"
                )
                return 1
    except SlaveConnectionError as e:
        console.print(f"[yellow]Warning: Cannot connect to slave: {e}[/yellow]")
        if not args.force:
            console.print("[yellow]Use --force to add anyway[/yellow]")
            return 1

    config.slaves.append(new_slave)

    if len(config.slaves) == 1:
        config.default_slave = args.name

    config.save_global()
    console.print(f"[green]Added slave '{args.name}'[/green]")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    """Remove a slave from configuration."""
    config = Config.load()

    found = False
    config.slaves = [s for s in config.slaves if s.name != args.name or not (found := True)]

    if not found:
        console.print(f"[red]Slave '{args.name}' not found[/red]")
        return 1

    if config.default_slave == args.name:
        config.default_slave = config.slaves[0].name if config.slaves else None

    config.save_global()
    console.print(f"[green]Removed slave '{args.name}'[/green]")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize project-local config."""
    project_path = Path(args.path).resolve()
    config_path = project_path / ".ci-farm.yaml"

    if config_path.exists() and not args.force:
        console.print(f"[yellow]Config already exists: {config_path}[/yellow]")
        console.print("[yellow]Use --force to overwrite[/yellow]")
        return 1

    build_cmd = detect_build_command(project_path)

    content = f"""# CI Farm project configuration
project:
  # Build command (auto-detected: {build_cmd or 'none'})
  build_command: {build_cmd or '"make"'}

  # Commands to run before syncing
  pre_sync: []

  # Commands to run after successful build
  post_build: []

  # Files/directories to exclude from sync
  exclude:
    - .git
    - __pycache__
    - "*.pyc"
    - node_modules
    - .venv
    - venv
    - .env
    - "*.egg-info"
    - dist
    - build

  # Build timeout in seconds
  timeout: 3600
"""

    config_path.write_text(content)
    console.print(f"[green]Created {config_path}[/green]")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    """Show current configuration."""
    project_path = Path(args.path).resolve() if args.path else None
    config = Config.load(project_path)

    console.print(f"[bold]Global config:[/bold] {GLOBAL_CONFIG_PATH}")
    if project_path:
        local_config = project_path / ".ci-farm.yaml"
        if local_config.exists():
            console.print(f"[bold]Local config:[/bold] {local_config}")

    console.print(f"\n[bold]Default slave:[/bold] {config.default_slave or 'none'}")
    console.print(f"[bold]Build command:[/bold] {config.project.build_command or 'auto-detect'}")
    console.print(f"[bold]Timeout:[/bold] {config.project.timeout}s")

    if config.project.exclude:
        console.print(f"[bold]Exclude:[/bold] {', '.join(config.project.exclude[:5])}...")

    return 0


def cmd_unlock(args: argparse.Namespace) -> int:
    """Force unlock a slave."""
    config = Config.load()
    slave = config.get_slave(args.name)

    if not slave:
        console.print(f"[red]Slave '{args.name}' not found[/red]")
        return 1

    try:
        with SlaveConnection(slave) as conn:
            lock_info = conn.get_lock_info()
            if lock_info:
                project, _ = lock_info
                console.print(f"[yellow]Releasing lock for '{project}'[/yellow]")
            conn.release_lock()
            console.print(f"[green]Unlocked '{args.name}'[/green]")
            return 0
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        return 1


def cmd_run(argv: list[str]) -> int:
    """Execute arbitrary command on a slave."""
    slave_name = None
    auto = False

    i = 0
    while i < len(argv):
        if argv[i] == "--on":
            if i + 1 < len(argv):
                slave_name = argv[i + 1]
                i += 2
                continue
            console.print("[red]--on requires a slave name[/red]")
            return 1
        if argv[i] == "--auto":
            auto = True
            i += 1
            continue
        if argv[i] == "--":
            i += 1
            break
        break

    remaining = argv[i:]
    if not remaining:
        console.print("[red]No command specified[/red]")
        return 1

    command = shlex.join(remaining)
    project_path = Path.cwd().resolve()

    config = Config.load(project_path)

    if not config.slaves:
        console.print("[red]No slaves configured. Run 'ci add' first.[/red]")
        return 1

    if not slave_name and auto:
        slave = find_available_slave(config.slaves)
        if slave:
            slave_name = slave.name
        else:
            console.print("[red]No available slaves found[/red]")
            return 1

    return execute_build(
        project_path,
        config,
        slave_name=slave_name,
        build_command=command,
        console=console,
    )


def create_parser() -> argparse.ArgumentParser:
    """Create argument parser."""
    parser = argparse.ArgumentParser(
        prog="ci",
        description="Simple distributed CI for local network devices",
        epilog=(
            "shorthand:\n"
            "  ci [--on SLAVE] [--auto] <command> [args...]\n"
            "  Run any command on a slave (syncs project first).\n"
            "\n"
            "examples:\n"
            "  ci make -j4\n"
            "  ci ./scripts/build-deb.sh --docker --distrib all\n"
            "  ci --on worker1 cargo build --release\n"
            "  ci --auto npm run build"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # build command
    build_parser = subparsers.add_parser("build", help="Run build on slave")
    build_parser.add_argument("path", nargs="?", default=".", help="Project path")
    build_parser.add_argument("--on", "-o", help="Slave name to use")
    build_parser.add_argument("--command", "-c", help="Override build command")
    build_parser.add_argument("--auto", "-a", action="store_true",
                             help="Auto-select available slave")

    # status command
    subparsers.add_parser("status", help="Show slaves status")

    # add command
    add_parser = subparsers.add_parser("add", help="Add a slave")
    add_parser.add_argument("name", help="Slave name")
    add_parser.add_argument("host", help="Host address")
    add_parser.add_argument("--user", "-u", default="root", help="SSH user")
    add_parser.add_argument("--port", "-p", type=int, default=22, help="SSH port")
    add_parser.add_argument("--key", "-k", help="SSH key path")
    add_parser.add_argument("--build-dir", "-d", default="/tmp/ci-farm-builds",
                           help="Remote build directory")
    add_parser.add_argument("--force", "-f", action="store_true",
                           help="Add even if connection fails")

    # remove command
    remove_parser = subparsers.add_parser("remove", help="Remove a slave")
    remove_parser.add_argument("name", help="Slave name")

    # init command
    init_parser = subparsers.add_parser("init", help="Initialize project config")
    init_parser.add_argument("path", nargs="?", default=".", help="Project path")
    init_parser.add_argument("--force", "-f", action="store_true", help="Overwrite existing")

    # config command
    config_parser = subparsers.add_parser("config", help="Show configuration")
    config_parser.add_argument("path", nargs="?", help="Project path")

    # unlock command
    unlock_parser = subparsers.add_parser("unlock", help="Force unlock a slave")
    unlock_parser.add_argument("name", help="Slave name")

    return parser


def main() -> int:
    """Main entry point."""
    if len(sys.argv) < 2:
        create_parser().print_help()
        return 0

    first_arg = sys.argv[1]

    if first_arg in ("--help", "-h", "--version"):
        create_parser().parse_args()
        return 0

    if first_arg in KNOWN_SUBCOMMANDS:
        parser = create_parser()
        args = parser.parse_args()

        commands = {
            "build": cmd_build,
            "status": cmd_status,
            "add": cmd_add,
            "remove": cmd_remove,
            "init": cmd_init,
            "config": cmd_config,
            "unlock": cmd_unlock,
        }

        handler = commands.get(args.command)
        if handler:
            return handler(args)

        parser.print_help()
        return 1

    return cmd_run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
