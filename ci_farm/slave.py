"""Slave device management via SSH."""

import socket
import time
from pathlib import Path
from typing import Callable, Optional

import paramiko

from .config import SlaveConfig

LOCK_FILE_NAME = ".ci-farm.lock"
CONNECTION_TIMEOUT = 10

DEFAULT_CHECK_TOOLS = [
    "python3",
    "gcc",
    "g++",
    "make",
    "cmake",
    "rsync",
    "git",
]


class SlaveConnectionError(Exception):
    """Failed to connect to slave."""


class SlaveBusyError(Exception):
    """Slave is currently busy with another build."""


class SlaveConnection:
    """Manages SSH connection to a slave device."""

    def __init__(self, config: SlaveConfig):
        self.config = config
        self.client: Optional[paramiko.SSHClient] = None
        self.sftp: Optional[paramiko.SFTPClient] = None

    def connect(self) -> None:
        """Establish SSH connection to the slave."""
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            "hostname": self.config.host,
            "port": self.config.port,
            "username": self.config.user,
            "timeout": CONNECTION_TIMEOUT,
        }

        if self.config.key:
            key_path = Path(self.config.key).expanduser()
            if key_path.exists():
                connect_kwargs["key_filename"] = str(key_path)
        elif self.config.password:
            connect_kwargs["password"] = self.config.password

        try:
            self.client.connect(**connect_kwargs)
            self.sftp = self.client.open_sftp()
        except (OSError, paramiko.SSHException, socket.timeout) as e:
            raise SlaveConnectionError(f"Failed to connect to {self.config.name}: {e}") from e

    def disconnect(self) -> None:
        """Close SSH connection."""
        if self.sftp:
            self.sftp.close()
            self.sftp = None
        if self.client:
            self.client.close()
            self.client = None

    def __enter__(self) -> "SlaveConnection":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.disconnect()

    def is_busy(self) -> bool:
        """Check if slave is currently running a build."""
        lock_path = f"{self.config.build_dir}/{LOCK_FILE_NAME}"
        try:
            self.sftp.stat(lock_path)
            return True
        except FileNotFoundError:
            return False

    def acquire_lock(self, project_name: str) -> None:
        """Create lock file on slave."""
        lock_path = f"{self.config.build_dir}/{LOCK_FILE_NAME}"
        self._ensure_dir(self.config.build_dir)

        lock_content = f"{project_name}\n{time.time()}\n"
        with self.sftp.file(lock_path, "w") as f:
            f.write(lock_content)

    def release_lock(self) -> None:
        """Remove lock file from slave."""
        lock_path = f"{self.config.build_dir}/{LOCK_FILE_NAME}"
        try:
            self.sftp.remove(lock_path)
        except FileNotFoundError:
            pass

    def _ensure_dir(self, path: str) -> None:
        """Create directory on slave if it doesn't exist."""
        try:
            self.sftp.stat(path)
        except FileNotFoundError:
            self.exec_command(f"mkdir -p {path}")

    def exec_command(
        self,
        command: str,
        working_dir: Optional[str] = None,
        timeout: Optional[int] = None,
        on_stdout: Optional[Callable[[str], None]] = None,
        on_stderr: Optional[Callable[[str], None]] = None,
    ) -> int:
        """Execute command on slave and stream output."""
        if working_dir:
            command = f"cd {working_dir} && {command}"

        _, stdout, stderr = self.client.exec_command(command, timeout=timeout)

        stdout_channel = stdout.channel
        stdout_channel.setblocking(False)

        exit_status = None
        stdout_buffer = ""
        stderr_buffer = ""

        while exit_status is None:
            if stdout_channel.recv_ready():
                chunk = stdout_channel.recv(1024).decode("utf-8", errors="replace")
                stdout_buffer += chunk
                while "\n" in stdout_buffer:
                    line, stdout_buffer = stdout_buffer.split("\n", 1)
                    if on_stdout:
                        on_stdout(line)

            if stdout_channel.recv_stderr_ready():
                chunk = stdout_channel.recv_stderr(1024).decode("utf-8", errors="replace")
                stderr_buffer += chunk
                while "\n" in stderr_buffer:
                    line, stderr_buffer = stderr_buffer.split("\n", 1)
                    if on_stderr:
                        on_stderr(line)

            if stdout_channel.exit_status_ready():
                exit_status = stdout_channel.recv_exit_status()
            else:
                time.sleep(0.1)

        if stdout_buffer and on_stdout:
            on_stdout(stdout_buffer)
        if stderr_buffer and on_stderr:
            on_stderr(stderr_buffer)

        return exit_status

    def check_tools(self, tools: list[str]) -> list[tuple[str, Optional[str]]]:
        """Check availability of tools on the slave."""
        tools_str = " ".join(tools)
        check_script = (
            f'for tool in {tools_str}; do '
            'if command -v "$tool" > /dev/null 2>&1; then '
            'ver=$("$tool" --version 2>&1 | head -1); '
            'echo "FOUND:$tool:$ver"; '
            'else echo "MISSING:$tool"; fi; done'
        )

        output_lines: list[str] = []
        self.exec_command(check_script, on_stdout=output_lines.append)

        results: list[tuple[str, Optional[str]]] = []
        for line in output_lines:
            if line.startswith("FOUND:"):
                parts = line.split(":", 2)
                name = parts[1] if len(parts) > 1 else ""
                version = parts[2] if len(parts) > 2 else ""
                results.append((name, version))
            elif line.startswith("MISSING:"):
                name = line.split(":", 1)[1] if ":" in line else ""
                results.append((name, None))

        return results

    def get_lock_info(self) -> Optional[tuple[str, float]]:
        """Get information about current lock."""
        lock_path = f"{self.config.build_dir}/{LOCK_FILE_NAME}"
        try:
            with self.sftp.file(lock_path, "r") as f:
                content = f.read().decode("utf-8")
                lines = content.strip().split("\n")
                if len(lines) >= 2:
                    return lines[0], float(lines[1])
        except FileNotFoundError:
            pass
        return None


def check_slave_available(config: SlaveConfig) -> tuple[bool, Optional[str]]:
    """Check if slave is available for builds."""
    try:
        with SlaveConnection(config) as conn:
            if conn.is_busy():
                lock_info = conn.get_lock_info()
                if lock_info:
                    project, timestamp = lock_info
                    elapsed = time.time() - timestamp
                    return False, f"Busy with '{project}' for {elapsed:.0f}s"
                return False, "Busy"
            return True, None
    except SlaveConnectionError as e:
        return False, str(e)


def find_available_slave(slaves: list[SlaveConfig]) -> Optional[SlaveConfig]:
    """Find first available slave from the list."""
    for slave in slaves:
        available, _ = check_slave_available(slave)
        if available:
            return slave
    return None
