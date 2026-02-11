"""Live monitoring dashboard for CI Farm slaves."""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from rich.columns import Columns
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from .config import Config, SlaveConfig
from .slave import SlaveConnection

BAR_WIDTH = 15
TEMP_WARN_THRESHOLD = 60.0
TEMP_CRIT_THRESHOLD = 75.0
USAGE_WARN_THRESHOLD = 60.0
USAGE_CRIT_THRESHOLD = 85.0
BYTES_IN_KB = 1024
SECONDS_IN_DAY = 86400
SECONDS_IN_HOUR = 3600
SECONDS_IN_MINUTE = 60
MILLIDEGREES_THRESHOLD = 1000.0
MAX_PERCENTAGE = 100.0
MIN_SLEEP = 0.1

METRICS_SCRIPT = (
    "echo '---LOADAVG---'; "
    "cat /proc/loadavg 2>/dev/null || echo 'N/A'; "
    "echo '---MEMINFO---'; "
    "grep -E '^(MemTotal|MemAvailable|MemFree|Buffers|Cached):' /proc/meminfo 2>/dev/null "
    "|| echo 'N/A'; "
    "echo '---UPTIME---'; "
    "cat /proc/uptime 2>/dev/null || echo 'N/A'; "
    "echo '---TEMP---'; "
    "cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null "
    "|| vcgencmd measure_temp 2>/dev/null || echo 'N/A'; "
    "echo '---DISK---'; "
    "df -k / 2>/dev/null | tail -1 || echo 'N/A'; "
    "echo '---UNAME---'; "
    "uname -snrm 2>/dev/null || echo 'N/A'; "
    "echo '---NPROC---'; "
    "nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 'N/A'; "
    "echo '---END---'"
)


@dataclass
class SlaveMetrics:
    """Collected metrics for a single slave."""

    name: str
    host: str
    user: str
    port: int
    online: bool = False
    error: Optional[str] = None
    os_info: str = ""
    cpu_cores: int = 0
    load_1: float = 0.0
    load_5: float = 0.0
    load_15: float = 0.0
    mem_total: int = 0
    mem_used: int = 0
    disk_total: int = 0
    disk_used: int = 0
    temperature: Optional[float] = None
    uptime_seconds: float = 0.0
    is_busy: bool = False
    busy_project: Optional[str] = None
    busy_duration: Optional[float] = None


# ---------------------------------------------------------------------------
#  Metric collection
# ---------------------------------------------------------------------------


def _get_connection(
    slave: SlaveConfig,
    conn_cache: dict[str, SlaveConnection],
) -> SlaveConnection:
    """Get or create a persistent SSH connection for a slave."""
    conn = conn_cache.get(slave.name)
    if conn and conn.client:
        transport = conn.client.get_transport()
        if transport and transport.is_active():
            return conn
        conn.disconnect()

    conn = SlaveConnection(slave)
    conn.connect()
    conn_cache[slave.name] = conn
    return conn


def _drop_connection(name: str, conn_cache: dict[str, SlaveConnection]) -> None:
    """Close and remove a cached connection."""
    conn = conn_cache.pop(name, None)
    if conn:
        try:
            conn.disconnect()
        except Exception:
            pass


def _collect_single(
    slave: SlaveConfig,
    conn_cache: dict[str, SlaveConnection],
) -> SlaveMetrics:
    """Collect metrics from a single slave over SSH."""
    metrics = SlaveMetrics(
        name=slave.name,
        host=slave.host,
        user=slave.user,
        port=slave.port,
    )

    try:
        conn = _get_connection(slave, conn_cache)

        output_lines: list[str] = []
        conn.exec_command(METRICS_SCRIPT, on_stdout=output_lines.append)
        _parse_metrics(output_lines, metrics)

        if conn.is_busy():
            metrics.is_busy = True
            lock_info = conn.get_lock_info()
            if lock_info:
                metrics.busy_project = lock_info[0]
                metrics.busy_duration = time.time() - lock_info[1]

        metrics.online = True
    except Exception as e:
        metrics.online = False
        metrics.error = str(e)
        _drop_connection(slave.name, conn_cache)

    return metrics


def _collect_all(
    slaves: list[SlaveConfig],
    conn_cache: dict[str, SlaveConnection],
) -> list[SlaveMetrics]:
    """Collect metrics from all slaves in parallel."""
    if not slaves:
        return []

    with ThreadPoolExecutor(max_workers=len(slaves)) as pool:
        futures = {
            pool.submit(_collect_single, slave, conn_cache): slave.name
            for slave in slaves
        }
        results: list[SlaveMetrics] = []
        for future in as_completed(futures):
            results.append(future.result())

    order = {s.name: i for i, s in enumerate(slaves)}
    results.sort(key=lambda m: order.get(m.name, 0))
    return results


# ---------------------------------------------------------------------------
#  Metric parsing
# ---------------------------------------------------------------------------


def _split_sections(output: list[str]) -> dict[str, list[str]]:
    """Split raw output into named sections by ``---NAME---`` markers."""
    sections: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in output:
        stripped = line.strip()
        if stripped.startswith("---") and stripped.endswith("---"):
            current = stripped.strip("-")
            sections[current] = []
        elif current is not None:
            sections[current].append(stripped)
    return sections


def _parse_metrics(output: list[str], metrics: SlaveMetrics) -> None:
    """Parse all metric sections from raw SSH output."""
    sections = _split_sections(output)
    _parse_loadavg(sections.get("LOADAVG", []), metrics)
    _parse_meminfo(sections.get("MEMINFO", []), metrics)
    _parse_uptime(sections.get("UPTIME", []), metrics)
    _parse_temp(sections.get("TEMP", []), metrics)
    _parse_disk(sections.get("DISK", []), metrics)
    _parse_uname(sections.get("UNAME", []), metrics)
    _parse_nproc(sections.get("NPROC", []), metrics)


def _parse_loadavg(lines: list[str], metrics: SlaveMetrics) -> None:
    if not lines or lines[0] == "N/A":
        return
    try:
        parts = lines[0].split()
        if len(parts) >= 3:
            metrics.load_1 = float(parts[0])
            metrics.load_5 = float(parts[1])
            metrics.load_15 = float(parts[2])
    except (ValueError, IndexError):
        pass


def _parse_meminfo(lines: list[str], metrics: SlaveMetrics) -> None:
    if not lines or lines[0] == "N/A":
        return
    try:
        mem: dict[str, int] = {}
        for line in lines:
            if ":" in line:
                key, value = line.split(":", 1)
                parts = value.strip().split()
                if parts:
                    mem[key.strip()] = int(parts[0]) * BYTES_IN_KB

        metrics.mem_total = mem.get("MemTotal", 0)
        available = mem.get("MemAvailable", 0)
        if available:
            metrics.mem_used = metrics.mem_total - available
        else:
            free = mem.get("MemFree", 0)
            buffers = mem.get("Buffers", 0)
            cached = mem.get("Cached", 0)
            metrics.mem_used = metrics.mem_total - free - buffers - cached
    except (ValueError, IndexError):
        pass


def _parse_uptime(lines: list[str], metrics: SlaveMetrics) -> None:
    if not lines or lines[0] == "N/A":
        return
    try:
        metrics.uptime_seconds = float(lines[0].split()[0])
    except (ValueError, IndexError):
        pass


def _parse_temp(lines: list[str], metrics: SlaveMetrics) -> None:
    if not lines or lines[0] == "N/A":
        return
    line = lines[0]
    try:
        if "temp=" in line:
            temp_part = line.split("=", 1)[1]
            digits: list[str] = []
            for ch in temp_part:
                if ch.isdigit() or ch == ".":
                    digits.append(ch)
                else:
                    break
            metrics.temperature = float("".join(digits))
        else:
            value = float(line)
            if value > MILLIDEGREES_THRESHOLD:
                metrics.temperature = value / MILLIDEGREES_THRESHOLD
            else:
                metrics.temperature = value
    except (ValueError, IndexError):
        pass


def _parse_disk(lines: list[str], metrics: SlaveMetrics) -> None:
    if not lines or lines[0] == "N/A":
        return
    try:
        parts = lines[0].split()
        if len(parts) >= 4:
            metrics.disk_total = int(parts[1]) * BYTES_IN_KB
            metrics.disk_used = int(parts[2]) * BYTES_IN_KB
    except (ValueError, IndexError):
        pass


def _parse_uname(lines: list[str], metrics: SlaveMetrics) -> None:
    if not lines or lines[0] == "N/A":
        return
    metrics.os_info = lines[0]


def _parse_nproc(lines: list[str], metrics: SlaveMetrics) -> None:
    if not lines or lines[0] == "N/A":
        return
    try:
        metrics.cpu_cores = int(lines[0])
    except ValueError:
        pass


# ---------------------------------------------------------------------------
#  Rendering helpers
# ---------------------------------------------------------------------------


def _usage_color(percentage: float) -> str:
    """Return a Rich color name based on usage percentage."""
    if percentage >= USAGE_CRIT_THRESHOLD:
        return "red"
    if percentage >= USAGE_WARN_THRESHOLD:
        return "yellow"
    return "green"


def _temp_color(temp: float) -> str:
    """Return a Rich color name based on temperature."""
    if temp >= TEMP_CRIT_THRESHOLD:
        return "red"
    if temp >= TEMP_WARN_THRESHOLD:
        return "yellow"
    return "green"


def _make_bar(percentage: float, width: int = BAR_WIDTH) -> str:
    """Build a colored Rich-markup progress bar."""
    percentage = max(0.0, min(MAX_PERCENTAGE, percentage))
    filled = int(width * percentage / MAX_PERCENTAGE)
    empty = width - filled
    color = _usage_color(percentage)
    return f"[{color}]{'█' * filled}[/{color}][dim]{'░' * empty}[/dim]"


def _format_bytes(n: int) -> str:
    """Format a byte count as a human-readable string."""
    if n == 0:
        return "0 B"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(n)
    for unit in units[:-1]:
        if abs(value) < BYTES_IN_KB:
            return f"{value:.1f} {unit}"
        value /= BYTES_IN_KB
    return f"{value:.1f} {units[-1]}"


def _format_uptime(seconds: float) -> str:
    """Format seconds as a human-readable uptime string."""
    days = int(seconds // SECONDS_IN_DAY)
    hours = int((seconds % SECONDS_IN_DAY) // SECONDS_IN_HOUR)
    minutes = int((seconds % SECONDS_IN_HOUR) // SECONDS_IN_MINUTE)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _percentage(used: float, total: float) -> float:
    """Calculate usage percentage safely."""
    if total <= 0:
        return 0.0
    return min(MAX_PERCENTAGE, (used / total) * MAX_PERCENTAGE)


# ---------------------------------------------------------------------------
#  Dashboard layout
# ---------------------------------------------------------------------------


def _build_slave_panel(metrics: SlaveMetrics) -> Panel:
    """Build a Rich Panel showing one slave's metrics."""
    lines: list[str] = []

    if not metrics.online:
        lines.append(f"[dim]{metrics.user}@{metrics.host}:{metrics.port}[/dim]")
        lines.append("")
        lines.append(f"[red]{metrics.error or 'Connection failed'}[/red]")
        return Panel(
            "\n".join(lines),
            title="[red]●[/red] " + metrics.name,
            title_align="left",
            border_style="red",
            padding=(0, 1),
        )

    # Host / OS
    lines.append(f"[dim]{metrics.user}@{metrics.host}[/dim]  [dim]{metrics.os_info}[/dim]")
    lines.append("")

    # CPU (load-based approximation)
    cpu_pct = _percentage(metrics.load_1, max(1, metrics.cpu_cores))
    cpu_bar = _make_bar(cpu_pct)
    cores_label = f"[dim]{metrics.cpu_cores}c[/dim]" if metrics.cpu_cores else ""
    lines.append(f"[bold]CPU [/bold] {cpu_bar} {cpu_pct:4.0f}%  {cores_label}")

    # Memory
    mem_pct = _percentage(metrics.mem_used, metrics.mem_total)
    mem_bar = _make_bar(mem_pct)
    lines.append(f"[bold]MEM [/bold] {mem_bar} {mem_pct:4.0f}%")
    lines.append(
        f"[dim]      {_format_bytes(metrics.mem_used)} / "
        f"{_format_bytes(metrics.mem_total)}[/dim]"
    )

    # Disk
    disk_pct = _percentage(metrics.disk_used, metrics.disk_total)
    disk_bar = _make_bar(disk_pct)
    lines.append(f"[bold]DISK[/bold] {disk_bar} {disk_pct:4.0f}%")
    lines.append(
        f"[dim]      {_format_bytes(metrics.disk_used)} / "
        f"{_format_bytes(metrics.disk_total)}[/dim]"
    )

    lines.append("")

    # Temperature + load average
    if metrics.temperature is not None:
        tc = _temp_color(metrics.temperature)
        temp_str = f"[{tc}]{metrics.temperature:.1f}°C[/{tc}]"
    else:
        temp_str = "[dim]N/A[/dim]"
    lines.append(
        f"[bold]Temp[/bold] {temp_str}    "
        f"[bold]Load[/bold] [dim]{metrics.load_1:.2f} / "
        f"{metrics.load_5:.2f} / {metrics.load_15:.2f}[/dim]"
    )

    # Uptime
    lines.append(f"[bold]Up  [/bold] {_format_uptime(metrics.uptime_seconds)}")

    # Active build
    if metrics.is_busy:
        project = metrics.busy_project or "unknown"
        build_line = f"Build: {project}"
        if metrics.busy_duration is not None:
            build_line += f" ({_format_uptime(metrics.busy_duration)})"
        lines.append(f"[yellow]{build_line}[/yellow]")

    # Border colour depends on state
    if metrics.is_busy:
        dot, border = "[yellow]●[/yellow]", "yellow"
    else:
        dot, border = "[green]●[/green]", "green"

    return Panel(
        "\n".join(lines),
        title=f"{dot} {metrics.name}",
        title_align="left",
        border_style=border,
        padding=(0, 1),
    )


def _build_header(all_metrics: list[SlaveMetrics], refresh_interval: int) -> Panel:
    """Build the summary header panel."""
    total = len(all_metrics)
    online = sum(1 for m in all_metrics if m.online)
    offline = total - online
    building = sum(1 for m in all_metrics if m.is_busy)
    now = datetime.now().strftime("%H:%M:%S")

    parts = [f"[green]● {online} online[/green]"]
    if offline:
        parts.append(f"[red]● {offline} offline[/red]")
    if building:
        parts.append(f"[yellow]● {building} building[/yellow]")

    return Panel(
        "    ".join(parts),
        title=(
            f"[bold cyan]CI Farm Monitor[/bold cyan]"
            f" ── {total} slaves ── ↻ {refresh_interval}s"
        ),
        subtitle=f"[dim]{now}[/dim]",
        subtitle_align="right",
        border_style="cyan",
        padding=(0, 1),
    )


def _build_dashboard(all_metrics: list[SlaveMetrics], refresh_interval: int) -> Group:
    """Compose the full monitoring dashboard."""
    header = _build_header(all_metrics, refresh_interval)
    panels = [_build_slave_panel(m) for m in all_metrics]
    columns = Columns(panels, equal=True, expand=True)
    footer = Text("Press Ctrl+C to exit", style="dim", justify="center")
    return Group(header, "", columns, "", footer)


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------


def run_monitor(config: Config, refresh_interval: int, console: Console) -> int:
    """Run the live monitoring dashboard with auto-refresh."""
    conn_cache: dict[str, SlaveConnection] = {}

    try:
        with Live(console=console, refresh_per_second=2, screen=True) as live:
            live.update(
                Text("Collecting metrics...", style="dim italic", justify="center")
            )

            while True:
                start = time.monotonic()
                metrics = _collect_all(config.slaves, conn_cache)
                dashboard = _build_dashboard(metrics, refresh_interval)
                live.update(dashboard)
                elapsed = time.monotonic() - start
                time.sleep(max(MIN_SLEEP, refresh_interval - elapsed))
    except KeyboardInterrupt:
        pass
    finally:
        for conn in conn_cache.values():
            try:
                conn.disconnect()
            except Exception:
                pass

    return 0
