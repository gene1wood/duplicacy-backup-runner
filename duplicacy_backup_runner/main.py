"""Run a scheduled Duplicacy backup, reporting progress to healthchecks.io.

Python port of run-scheduled-duplicacy-backup.bash.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import dataclasses
import enum
import json
import logging
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import platformdirs
import requests
import yaml

from duplicacy_backup_runner import __version__

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import msvcrt
else:
    import fcntl

CONFIG_APP_NAME = "duplicacy-backup-runner"
HEALTHCHECKS_BASE_URL = "https://hc-ping.com"
PRUNE_KEEP_ARGS = ["-keep", "0:360", "-keep", "30:180", "-keep", "7:30", "-keep", "1:7"]
DEFAULT_DUPLICACY_VERSION = "3.2.5"
DEFAULT_DUPLICACY_BASEDIR = Path("C:/duplicacy" if IS_WINDOWS else "/opt/duplicacy")
# Where the Windows OpenSSH Client optional feature installs sftp
WINDOWS_SFTP_PATH = Path("C:/Windows/System32/OpenSSH/sftp.exe")


@dataclasses.dataclass
class Config:
    healthchecks_uuid: str
    client_individual_id: str
    backup_directories: list[Path]
    duplicacy_basedir: Path
    log_basedir: Path
    lock_file: Path
    rate_limit_ip: str | None
    rate_limit_rate: int
    known_hosts_string: str | None
    log_level: str
    internet_check_attempts: int
    internet_check_delay: int
    internet_check_url: str
    duplicacy_version: str
    duplicacy_download_url: str | None
    filters_url: str | None
    # Set from --dry-run, never from config.yaml
    dry_run: bool = False

    @property
    def duplicacy_binary(self) -> Path:
        return duplicacy_binary_path(self.duplicacy_basedir)


def duplicacy_binary_path(duplicacy_basedir: Path) -> Path:
    return duplicacy_basedir / "bin" / ("duplicacy.exe" if IS_WINDOWS else "duplicacy")


def short_hostname() -> str:
    """This host's name without its domain, used as the default
    client_individual_id and in log file names. Lowercased on Windows,
    whose hostnames are conventionally uppercase."""
    name = socket.gethostname().split(".")[0]
    return name.lower() if IS_WINDOWS else name


def default_config_path() -> Path:
    """On Windows, config.yaml lives in DEFAULT_DUPLICACY_BASEDIR rather than
    a per-user platformdirs directory, because setup runs as an
    administrator but the scheduled task runs as SYSTEM, and each would
    otherwise look in its own profile."""
    if IS_WINDOWS:
        return DEFAULT_DUPLICACY_BASEDIR / "config.yaml"
    return platformdirs.user_config_path(CONFIG_APP_NAME) / "config.yaml"


def load_config(path: Path | None) -> Config:
    """Loads and validates config.yaml, applying defaults for everything
    except healthchecks_uuid, which must be set.

    :param path: Explicit config path, or None to use the platformdirs
        default location.
    :returns: The parsed configuration.
    """
    config_path = path or default_config_path()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise SystemExit(f"Config file not found: {config_path}")

    healthchecks_uuid = raw.get("healthchecks_uuid")
    if not healthchecks_uuid:
        raise SystemExit(
            f"Config isn't set. Aborting (missing healthchecks_uuid in {config_path})"
        )

    duplicacy_basedir = Path(raw.get("duplicacy_basedir", DEFAULT_DUPLICACY_BASEDIR))
    default_lock_file = (
        duplicacy_basedir / "duplicacy-backup.lock"
        if IS_WINDOWS
        else Path("/var/lock/duplicacy-backup")
    )

    return Config(
        healthchecks_uuid=healthchecks_uuid,
        client_individual_id=raw.get("client_individual_id", short_hostname()),
        backup_directories=[
            Path(d)
            for d in raw.get(
                "backup_directories", [DEFAULT_DUPLICACY_BASEDIR / "backup"]
            )
        ],
        duplicacy_basedir=duplicacy_basedir,
        log_basedir=Path(raw.get("log_basedir", duplicacy_basedir / "logs")),
        lock_file=Path(raw.get("lock_file", default_lock_file)),
        rate_limit_ip=raw.get("rate_limit_ip"),
        rate_limit_rate=raw.get("rate_limit_rate", 32),
        known_hosts_string=raw.get("known_hosts_string"),
        log_level=raw.get("log_level", "INFO"),
        internet_check_attempts=raw.get("internet_check_attempts", 5),
        internet_check_delay=raw.get("internet_check_delay", 5),
        internet_check_url=raw.get("internet_check_url", "http://www.google.com/"),
        duplicacy_version=raw.get("duplicacy_version", DEFAULT_DUPLICACY_VERSION),
        duplicacy_download_url=raw.get("duplicacy_download_url"),
        filters_url=raw.get("filters_url"),
    )


class RunLogger:
    """Writes status lines to the run log, the persistent per-client log, the
    lastrun log, and the console, while raw subprocess output (duplicacy and
    sftp output) is written only to the run log, lastrun log, and console.

    Passing None for all three log paths (as a dry run does, so it leaves
    every log file untouched) logs to the console only."""

    def __init__(
        self,
        run_log_path: Path | None,
        persistent_log_path: Path | None,
        lastrun_log_path: Path | None,
        level: int,
    ) -> None:
        file_paths = [
            path
            for path in (run_log_path, persistent_log_path, lastrun_log_path)
            if path is not None
        ]
        # Only echo raw subprocess output to a real terminal. Backups can run for
        # hours and produce thousands of lines; under cron/systemd that output
        # would otherwise flood a mailed job log or the journal for no benefit,
        # since the files below are already the durable record. With no files
        # (a dry run), the console is the only record, so always echo.
        self._echo_raw = sys.stdout.isatty() or not file_paths

        self._logger = logging.getLogger("duplicacy_backup_runner")
        self._logger.setLevel(logging.DEBUG)
        self._logger.handlers.clear()
        self._logger.propagate = False

        formatter = logging.Formatter(
            fmt="%(asctime)s.%(msecs)03d %(levelname)s PARENT_UPDATE %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handlers: list[logging.Handler] = []
        self._raw_streams = []
        self._file_handlers: dict[Path, logging.FileHandler] = {}
        for path in file_paths:
            file_handler = logging.FileHandler(path, encoding="utf-8")
            handlers.append(file_handler)
            self._file_handlers[path] = file_handler
            if path != persistent_log_path:
                self._raw_streams.append(file_handler.stream)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        handlers.append(console_handler)

        for handler in handlers:
            handler.setFormatter(formatter)
            self._logger.addHandler(handler)

    def info(self, message: str) -> None:
        self._logger.info(message)

    def error(self, message: str) -> None:
        self._logger.error(message)

    def debug(self, message: str) -> None:
        self._logger.debug(message)

    def close_log_file(self, path: Path) -> None:
        """Stops logging to path and closes it, so it can be deleted -- on
        Windows a file can't be deleted while it's still open. Later lines
        still go to the remaining log files and the console."""
        file_handler = self._file_handlers.pop(path, None)
        if file_handler is None:
            return
        self._logger.removeHandler(file_handler)
        if file_handler.stream in self._raw_streams:
            self._raw_streams.remove(file_handler.stream)
        file_handler.close()

    def write_raw(self, line: str) -> None:
        """Write a line of unprefixed subprocess output to the run log and
        lastrun log only, matching the persistent client log staying free of
        per-file backup noise.

        Flushed immediately (as logging's handlers already do for formatted
        status lines) so a run that's killed mid-backup still leaves every
        line processed so far on disk, rather than buffered in the dying
        process."""
        for stream in self._raw_streams:
            stream.write(line + "\n")
            stream.flush()
        if self._echo_raw:
            print(line)


def wait_for_internet(
    run_logger: RunLogger,
    attempts: int = 5,
    delay: int = 5,
    url: str = "http://www.google.com/",
) -> bool:
    """Best-effort connectivity probe: polls url up to `attempts` times,
    sleeping `delay` seconds between failures. Never raises and never
    blocks the run -- a persistent failure is only logged at debug level,
    since duplicacy itself will fail loudly enough if there's truly no
    network."""
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                if response.status < 400:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        if attempt < attempts - 1:
            time.sleep(delay)
    run_logger.debug(
        f"Internet connectivity check did not succeed after {attempts} attempts"
    )
    return False


def resolve_rate_limit(rate_limit_ip: str | None, rate_limit_rate: int) -> int | None:
    """Returns the configured rate limit in KB/s if this host has
    rate_limit_ip configured on any network interface, otherwise None
    (unlimited). Compares exact addresses (via `ip -json addr show`, or on
    Windows, which has no `ip`, the addresses this host's name resolves to)
    rather than a substring match, since e.g. "192.168.1.5" is a substring
    of "192.168.1.50" and must not match it."""
    if not rate_limit_ip:
        return None
    if IS_WINDOWS:
        try:
            host_ips = {
                info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None)
            }
        except OSError:
            return None
        return rate_limit_rate if rate_limit_ip in host_ips else None
    result = subprocess.run(
        ["ip", "-json", "addr", "show"], capture_output=True, text=True, check=False
    )
    try:
        interfaces = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    host_ips = {
        addr["local"]
        for interface in interfaces
        for addr in interface.get("addr_info", [])
    }
    return rate_limit_rate if rate_limit_ip in host_ips else None


def ensure_known_hosts(
    duplicacy_basedir: Path,
    known_hosts_string: str | None,
    write_dir: Path | None = None,
) -> Path:
    """Writes known_hosts_string to <duplicacy_basedir>/keys/known_hosts if
    it isn't already there verbatim, so sftp connections trust the expected
    hosts without rewriting the file (and its mtime) on every run.

    :param write_dir: Directory to write known_hosts into instead of
        <duplicacy_basedir>/keys -- a dry run passes a scratch directory so
        its sftp checks trust the configured hosts without touching the real
        file.
    :returns: The known_hosts path sftp should use.
    """
    known_hosts_path = (write_dir or duplicacy_basedir / "keys") / "known_hosts"
    if known_hosts_string:
        try:
            current = known_hosts_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = None
        if current != known_hosts_string:
            known_hosts_path.write_text(known_hosts_string, encoding="utf-8")
    return known_hosts_path


def default_duplicacy_download_url(version: str) -> str:
    """The x64 GitHub release asset for this platform, e.g.
    duplicacy_linux_x64_3.2.5 or duplicacy_win_x64_3.2.5.exe."""
    os_name = "win" if IS_WINDOWS else "linux"
    suffix = ".exe" if IS_WINDOWS else ""
    return (
        "https://github.com/gilbertchen/duplicacy/releases/download/"
        f"v{version}/duplicacy_{os_name}_x64_{version}{suffix}"
    )


def download_file(url: str, destination: Path) -> None:
    """Streams url to destination -- shared by provision_duplicacy_binary
    and setup's filters/binary fetches."""
    with urllib.request.urlopen(url, timeout=30) as response:
        destination.write_bytes(response.read())


def provision_basedir(
    duplicacy_basedir: Path,
    log_basedir: Path,
    run_logger: RunLogger,
    dry_run: bool = False,
) -> None:
    """Ensures duplicacy_basedir/{bin,keys} and log_basedir exist, with keys
    chmod 700. Logged, not raised, on failure -- but a failure here is
    effectively fatal for this run's own next steps, which will fail loudly
    on their own. A dry run only logs what it would create or chmod."""
    keys_dir = duplicacy_basedir / "keys"
    if dry_run:
        for directory in (duplicacy_basedir / "bin", keys_dir, log_basedir):
            if not directory.is_dir():
                run_logger.info(f"provision: dry run, would create {directory}")
        if keys_dir.is_dir():
            chmod_if_needed(keys_dir, 0o700, run_logger, dry_run=True)
        return
    try:
        (duplicacy_basedir / "bin").mkdir(parents=True, exist_ok=True)
        keys_dir.mkdir(parents=True, exist_ok=True)
        keys_dir.chmod(0o700)
        log_basedir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        run_logger.error(f"provision: unable to create base directories: {error}")


def provision_duplicacy_binary(
    duplicacy_binary: Path,
    version: str,
    download_url: str | None,
    run_logger: RunLogger,
    dry_run: bool = False,
) -> None:
    """Downloads the duplicacy binary to duplicacy_binary (chmod 755) if
    it's not already there, from download_url or the URL derived from
    version via default_duplicacy_download_url."""
    if duplicacy_binary.exists():
        return
    url = download_url or default_duplicacy_download_url(version)
    if dry_run:
        run_logger.info(
            f"provision: dry run, would download duplicacy binary from {url} "
            f"to {duplicacy_binary}"
        )
        return
    run_logger.info(f"provision: downloading duplicacy binary from {url}")
    try:
        download_file(url, duplicacy_binary)
        duplicacy_binary.chmod(0o755)
    except (urllib.error.URLError, OSError) as error:
        run_logger.error(f"provision: failed to download duplicacy binary: {error}")


def chmod_if_needed(
    path: Path, mode: int, run_logger: RunLogger, dry_run: bool = False
) -> None:
    """Chmods path to mode only if it isn't already that mode (or, in a dry
    run, only logs that it would). A no-op on Windows, where chmod only
    toggles the read-only attribute: key file access there is controlled by
    the ACL that setup applies (see setup.secure_key_file)."""
    if IS_WINDOWS:
        return
    try:
        if stat.S_IMODE(path.stat().st_mode) == mode:
            return
        if dry_run:
            run_logger.info(f"provision: dry run, would chmod {path} to {mode:o}")
        else:
            path.chmod(mode)
    except OSError as error:
        run_logger.error(f"provision: unable to chmod {path}: {error}")


def provision_ssh_key_permissions(
    backup_directory: Path, run_logger: RunLogger, dry_run: bool = False
) -> None:
    """Defensively chmods 600 any keys.ssh_key_file already referenced in
    backup_directory's preferences (e.g. restored with looser permissions)
    -- sftp/ssh refuse a group/world-readable private key. No-op if there's
    no preferences file yet."""
    preferences_path = backup_directory / ".duplicacy" / "preferences"
    if not preferences_path.is_file():
        return
    try:
        entries = read_storage_entries(backup_directory)
    except (OSError, json.JSONDecodeError, KeyError) as error:
        run_logger.error(f"provision: unable to read {preferences_path}: {error}")
        return
    for entry in entries:
        if entry.ssh_key_file and entry.ssh_key_file.is_file():
            chmod_if_needed(entry.ssh_key_file, 0o600, run_logger, dry_run)


def provision_filters(
    backup_directory: Path,
    filters_url: str | None,
    run_logger: RunLogger,
    dry_run: bool = False,
) -> None:
    """Downloads .duplicacy/filters from filters_url if configured and the
    file is missing. There's no default URL -- only acts when explicitly
    configured."""
    if not filters_url:
        return
    filters_path = backup_directory / ".duplicacy" / "filters"
    if filters_path.exists() or not filters_path.parent.is_dir():
        return
    if dry_run:
        run_logger.info(
            f"provision: dry run, would download filters from {filters_url} to "
            f"{filters_path}"
        )
        return
    try:
        run_logger.info(f"provision: downloading filters to {filters_path}")
        download_file(filters_url, filters_path)
    except (urllib.error.URLError, OSError) as error:
        run_logger.error(f"provision: failed to download filters: {error}")


def provision(config: Config, run_logger: RunLogger) -> None:
    """Best-effort, idempotent setup steps safe to repeat on every scheduled
    run: ensures duplicacy_basedir/{bin,keys} and log_basedir exist,
    downloads the duplicacy binary if missing, defensively chmods 600 any
    ssh_key_file already referenced in a backup directory's preferences, and
    downloads .duplicacy/filters if configured and missing. Failures are
    logged via run_logger, not raised -- downstream duplicacy invocations
    (and ensure_known_hosts) fail loudly and specifically if something here
    still didn't work out, matching wait_for_internet's best-effort style.
    A dry run only logs what each step would do."""
    provision_basedir(
        config.duplicacy_basedir, config.log_basedir, run_logger, config.dry_run
    )
    provision_duplicacy_binary(
        config.duplicacy_binary,
        config.duplicacy_version,
        config.duplicacy_download_url,
        run_logger,
        config.dry_run,
    )
    for backup_directory in config.backup_directories:
        if not backup_directory.is_dir():
            continue
        provision_ssh_key_permissions(backup_directory, run_logger, config.dry_run)
        provision_filters(
            backup_directory, config.filters_url, run_logger, config.dry_run
        )


def ping_healthchecks(
    healthchecks_uuid: str,
    run_uuid: str,
    suffix: str = "",
    data: str | None = None,
    retries: int = 3,
) -> bool:
    """POSTs one healthchecks.io ping (start/fail/log/success, per suffix),
    retrying transient failures up to `retries` times. Returns whether it
    ever succeeded; never raises, since a healthchecks outage shouldn't
    abort the backup itself."""
    url = f"{HEALTHCHECKS_BASE_URL}/{healthchecks_uuid}{suffix}"
    for _ in range(retries):
        try:
            response = requests.post(
                url, params={"rid": run_uuid}, data=data, timeout=10
            )
            response.raise_for_status()
        except requests.RequestException:
            continue
        return True
    return False


def notify_healthchecks(
    config: Config, run_uuid: str, suffix: str = "", data: str | None = None
) -> None:
    """ping_healthchecks for this run, unless it's a dry run -- a dry run
    must never register a start, failure, or success with healthchecks.io."""
    if config.dry_run:
        return
    ping_healthchecks(config.healthchecks_uuid, run_uuid, suffix, data=data)


def log_error(
    run_logger: RunLogger, config: Config, run_uuid: str, message: str
) -> None:
    notify_healthchecks(config, run_uuid, "/fail", data=message)
    run_logger.error(message)


def is_empty_backup_directory(backup_directory: Path) -> bool:
    """True if backup_directory contains no entries at all -- distinct from
    has_non_symlink_directories, which only fires on a real (non-symlink)
    subdirectory. An empty backup_directory also has no
    .duplicacy/preferences yet, so read_storage_entries would otherwise
    raise an uncaught FileNotFoundError."""
    return not any(backup_directory.iterdir())


def has_non_symlink_directories(backup_directory: Path) -> bool:
    """True if backup_directory contains a real (non-symlink) subdirectory
    other than .duplicacy -- such a directory would hide any symlinks
    nested inside it, since Duplicacy only follows top-level symlinks."""
    return any(
        entry.is_dir() and not entry.is_symlink()
        for entry in backup_directory.iterdir()
        if entry.name != ".duplicacy"
    )


def is_local_storage(storage: str) -> bool:
    return "://" not in storage


@dataclasses.dataclass(frozen=True)
class StorageEntry:
    """One destination from a repository's .duplicacy/preferences file."""

    name: str
    storage: str
    encrypted: bool
    ssh_key_file: Path | None
    password: str | None
    id: str | None = None

    @property
    def is_local(self) -> bool:
        return is_local_storage(self.storage)

    @classmethod
    def from_preferences(cls, raw: dict) -> StorageEntry:
        keys = raw.get("keys") or {}
        ssh_key_file = keys.get("ssh_key_file")
        return cls(
            name=raw["name"],
            storage=raw["storage"],
            encrypted=bool(raw.get("encrypted")),
            ssh_key_file=Path(ssh_key_file) if ssh_key_file else None,
            password=keys.get("password") or None,
            id=raw.get("id"),
        )


def read_storage_entries(backup_directory: Path) -> list[StorageEntry]:
    """Returns every destination entry from the repository's duplicacy
    preferences file (there's one entry per storage the repository backs up
    to, "default" being the first one added)."""
    raw_entries = json.loads(
        (backup_directory / ".duplicacy" / "preferences").read_text(encoding="utf-8")
    )
    return [StorageEntry.from_preferences(raw) for raw in raw_entries]


def find_local_entry(entries: list[StorageEntry]) -> StorageEntry | None:
    return next((entry for entry in entries if entry.is_local), None)


def derive_remote_name(storage: str) -> str:
    """The last path segment of a storage URL, e.g. "sftp://host/backup" ->
    "backup". Used both to look up directory ownership over sftp and as the
    remote root under which run logs get uploaded, so that destinations
    whose remote path isn't literally named "backup" still work."""
    segments = [
        segment for segment in urllib.parse.urlparse(storage).path.split("/") if segment
    ]
    return segments[-1] if segments else ""


def parse_sftp_target(storage: str) -> tuple[str | None, str | None, int]:
    parsed = urllib.parse.urlparse(storage)
    return parsed.username, parsed.hostname, parsed.port or 22


def sftp_run(
    commands: str,
    user: str,
    host: str,
    port: int,
    key_file: Path,
    known_hosts: Path,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """Runs `commands` (newline-separated sftp batch-mode commands, e.g.
    "pwd\\n" or "put ...\\nrm ...\\n") against one sftp server, returning
    the completed process rather than raising on a non-zero exit -- callers
    decide what a failure means here, including a timeout, which is
    reported as returncode -1.

    Output goes to temporary files rather than pipes: on Windows sftp.exe
    runs ssh.exe as a child that holds sftp's output handles, so reading
    pipes to EOF (as subprocess.run does, even after killing sftp on a
    timeout) can block forever while ssh.exe lingers."""
    cmd = [
        sftp_executable(),
        "-o",
        f"Port={port}",
        "-o",
        f"IdentityFile={key_file}",
        "-o",
        "BatchMode=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        f"{user}@{host}",
    ]
    with (
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=stdout_file, stderr=stderr_file
        )
        assert process.stdin is not None
        with contextlib.suppress(OSError):
            # sftp may exit (e.g. on a failed connection) before reading it all
            process.stdin.write(commands.encode("utf-8"))
        with contextlib.suppress(OSError):
            process.stdin.close()
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_process_tree(process)
            process.wait()
            returncode = -1
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read().decode("utf-8", errors="replace")
        stderr = stderr_file.read().decode("utf-8", errors="replace")
    if returncode == -1:
        stderr += f"\nsftp timed out after {timeout} seconds"
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


def kill_process_tree(process: subprocess.Popen) -> None:
    """Kills process and, on Windows, its children too (e.g. the ssh.exe
    that sftp.exe starts), which process.kill() alone would leave running."""
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
    process.kill()


def sftp_executable() -> str:
    """sftp from PATH, except on Windows, where the built-in OpenSSH
    Client's sftp is preferred if installed: a scheduled task running as
    SYSTEM may not have it on its PATH, and an sftp found first on PATH
    (e.g. Git for Windows' Unix tools) wouldn't necessarily handle Windows
    paths and key file ACLs the same way."""
    if IS_WINDOWS and WINDOWS_SFTP_PATH.is_file():
        return str(WINDOWS_SFTP_PATH)
    return shutil.which("sftp") or "sftp"


def test_sftp_connectivity(
    user: str, host: str, port: int, key_file: Path, known_hosts: Path
) -> bool:
    result = sftp_run("pwd\n", user, host, port, key_file, known_hosts)
    return result.returncode == 0


def parse_ls_ln_entry(ls_output: str, name: str) -> list[str] | None:
    """Returns the whitespace-split fields of the row for the entry named
    `name` in `sftp ls -ln` output, or None if no such entry is listed.
    Shared by parse_ls_ln_owner (which wants fields[2], the numeric owner
    uid) and ls_ln_lists_entry (which only cares whether a row exists at
    all). Only the last path component of each listed name is compared,
    because `ls -ln somedir` prefixes every entry with the directory it
    listed (e.g. "backup/chunks") whereas a bare `ls -ln` doesn't."""
    field_rows = (line.split() for line in ls_output.splitlines())
    return next(
        (
            fields
            for fields in field_rows
            if len(fields) >= 9 and fields[8].rsplit("/", 1)[-1] == name
        ),
        None,
    )


def parse_ls_ln_owner(ls_output: str, name: str = "backup") -> str | None:
    """Returns the numeric owner uid for the entry named `name` in `sftp
    ls -ln` output, or None if no such entry is listed."""
    fields = parse_ls_ln_entry(ls_output, name)
    return fields[2] if fields is not None else None


def ls_ln_lists_entry(ls_output: str, name: str) -> bool:
    """True if `sftp ls -ln` output lists an entry named `name` -- used to
    check for a subdirectory's presence (e.g. "chunks") when its owner is
    irrelevant, unlike parse_ls_ln_owner."""
    return parse_ls_ln_entry(ls_output, name) is not None


def get_remote_directory_owner(
    user: str,
    host: str,
    port: int,
    key_file: Path,
    known_hosts: Path,
    name: str = "backup",
) -> str | None:
    """Looks up the numeric owner uid of the remote directory `name`, to
    decide whether this host is allowed to prune/manage it (see
    can_modify_destination), or None if it can't be determined."""
    result = sftp_run("ls -ln\n", user, host, port, key_file, known_hosts)
    return parse_ls_ln_owner(result.stdout, name)


def storage_initialized_over_sftp(
    user: str, host: str, port: int, key_file: Path, known_hosts: Path, remote_root: str
) -> bool:
    """True if remote_root already contains a "chunks" subdirectory, meaning
    `duplicacy init` has already been run against this destination. False
    both when remote_root doesn't exist yet and when it exists but is
    empty -- either way, initialize_storage needs to run."""
    result = sftp_run(
        f'ls -ln "{remote_root}"\n', user, host, port, key_file, known_hosts
    )
    return result.returncode == 0 and ls_ln_lists_entry(result.stdout, "chunks")


def log_directory_contents(run_logger: RunLogger, backup_directory: Path) -> None:
    run_logger.info(f"Listing backup targets for {backup_directory}")
    for entry in sorted(backup_directory.iterdir()):
        run_logger.info(f"Backup targets : {entry.name} -> {entry.resolve()}")


def log_filters(run_logger: RunLogger, backup_directory: Path) -> None:
    filters_path = backup_directory / ".duplicacy" / "filters"
    run_logger.info(f"Filters for {backup_directory}")
    if filters_path.exists():
        for line in filters_path.read_text(encoding="utf-8").splitlines():
            run_logger.info(f"Filter line : {line}")


def run_duplicacy(
    args: list[str], cwd: Path, run_logger: RunLogger, env: dict[str, str] | None = None
) -> int:
    """Runs a duplicacy (or sftp) command with cwd set to the repository,
    streaming its combined stdout+stderr line-by-line into
    run_logger.write_raw as it's produced -- so a multi-hour run's output
    lands on disk incrementally rather than only at the end. Returns the
    exit code. env is passed through to subprocess.Popen unchanged (None
    means inherit this process's environment, e.g. for DUPLICACY_PASSWORD).
    stdin is /dev/null so a credential duplicacy wasn't given (e.g. "Enter
    the path of the private key file:") fails fast instead of hanging on a
    prompt that, having no trailing newline, never reaches the log."""
    process = subprocess.Popen(
        args,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    assert process.stdout is not None
    for line in process.stdout:
        run_logger.write_raw(line.rstrip("\n"))
    process.wait()
    return process.returncode


@dataclasses.dataclass
class SftpTarget:
    """An sftp destination that passed connectivity checks, tracked so the
    run log can be uploaded to the last one touched and so callers know
    whether they're allowed to modify it (see can_modify_destination)."""

    name: str
    client: str
    server: str
    port: int
    key_file: Path
    remote_root: str
    writable: bool


class EncryptionStatus(enum.Enum):
    """Whether a storage is RSA-encrypted, as determined by
    check_rsa_encryption. UNKNOWN is never treated as "probably not RSA" --
    see plan_destinations, which hard-fails rather than guessing."""

    RSA = "rsa"
    NOT_RSA = "not_rsa"
    UNKNOWN = "unknown"


@dataclasses.dataclass
class PreflightResult:
    """initialized is False only in a dry run, when the destination still
    needs `duplicacy init` -- which a dry run skips, leaving nothing for
    backup/copy/prune to run against."""

    ok: bool
    sftp_target: SftpTarget | None = None
    initialized: bool = True


@dataclasses.dataclass
class Outcome:
    """The result of processing one destination, or one backup_directory's
    worth of destinations: whether everything attempted succeeded, and the
    last sftp destination touched (for the end-of-run log upload)."""

    ok: bool
    sftp_target: SftpTarget | None = None


@dataclasses.dataclass
class PlannedDestination:
    """One step of a backup_directory's plan: back entry up directly
    (copy_source is None) or copy to it from the destination named
    copy_source."""

    entry: StorageEntry
    copy_source: str | None = None


def can_modify_destination(sftp_target: SftpTarget | None) -> bool:
    """True if we're allowed to prune or rewrite the "latest log" symlink on
    a destination: always true for local destinations (sftp_target is
    None), and true over sftp unless the remote directory is owned by root
    (typically meaning the server manages its own retention) or its
    ownership couldn't be determined."""
    return sftp_target is None or sftp_target.writable


def find_rsa_public_keys(keys_dir: Path) -> list[Path]:
    """Returns every file directly under keys_dir that looks like a PEM RSA
    public key, sniffed by content ("-----BEGIN PUBLIC KEY-----", the PKIX
    form `openssl rsa -pubout` produces, or the PKCS1 "-----BEGIN RSA
    PUBLIC KEY-----" form) since -- unlike the id_*_<hostname> SSH identity
    files -- there's no naming convention for it. Used by
    initialize_storage to intuit RSA usage for a destination that has no
    config file yet to ask Duplicacy about directly."""
    if not keys_dir.is_dir():
        return []
    matches = []
    for candidate in sorted(keys_dir.iterdir()):
        if not candidate.is_file():
            continue
        try:
            content = candidate.read_text(encoding="utf-8", errors="replace")
        except (UnicodeDecodeError, OSError):
            continue
        if "BEGIN PUBLIC KEY" in content or "BEGIN RSA PUBLIC KEY" in content:
            matches.append(candidate)
    return matches


def duplicacy_init_env(
    password: str | None, ssh_key_file: Path | None
) -> dict[str, str] | None:
    """The environment for a `duplicacy init`, which runs without a
    preferences file to read keys.password/keys.ssh_key_file from, so they
    have to be supplied as DUPLICACY_PASSWORD/DUPLICACY_SSH_KEY_FILE (the
    env var names duplicacy looks up for the "default" storage init
    creates) or duplicacy prompts for them. None when neither is needed."""
    extra = {}
    if password:
        extra["DUPLICACY_PASSWORD"] = password
    if ssh_key_file is not None:
        extra["DUPLICACY_SSH_KEY_FILE"] = str(ssh_key_file)
    return {**os.environ, **extra} if extra else None


def initialize_storage(
    entry: StorageEntry,
    storage_url: str,
    config: Config,
    run_logger: RunLogger,
    run_uuid: str,
) -> bool:
    """Runs `duplicacy init` directly against storage_url (the destination's
    storage, resolved to an absolute path by the caller if it's a relative
    local one -- init runs in a throwaway scratch directory rather than the
    real repository, so a path relative to the repository wouldn't resolve
    correctly on its own), to create the chunks/config/snapshots layout
    that preflight_destination found missing. Whether to pass -e/-key is
    taken from entry.encrypted and entry.password (already known from
    preferences) plus whether an RSA public key is found in
    duplicacy_basedir/keys via find_rsa_public_keys -- there's no config
    file yet to ask Duplicacy itself, unlike check_rsa_encryption, so this
    is the best this tool can intuit; more than one candidate key is
    refused rather than guessed. A dry run does all of this validation but
    only logs the init command rather than running it."""
    rsa_key: Path | None = None
    if entry.encrypted:
        if not entry.password:
            log_error(
                run_logger,
                config,
                run_uuid,
                f"Destination '{entry.name}' needs initializing but is missing "
                "keys.password in duplicacy preferences",
            )
            return False
        candidates = find_rsa_public_keys(config.duplicacy_basedir / "keys")
        if len(candidates) > 1:
            log_error(
                run_logger,
                config,
                run_uuid,
                f"Destination '{entry.name}' needs initializing but found multiple "
                f"candidate RSA public keys in {config.duplicacy_basedir / 'keys'}; "
                "refusing to guess which one to use",
            )
            return False
        rsa_key = candidates[0] if candidates else None

    init_args = [str(config.duplicacy_binary), "-log", "init"]
    if entry.encrypted:
        init_args.append("-e")
    if rsa_key is not None:
        init_args += ["-key", str(rsa_key)]
    init_args += [entry.id or config.client_individual_id, storage_url]

    if config.dry_run:
        run_logger.info(
            f"Dry run: destination '{entry.name}' has no chunks directory; would "
            f"initialize storage : duplicacy {' '.join(init_args[1:])}"
        )
        return True

    run_logger.info(
        f"Destination '{entry.name}' has no chunks directory; initializing storage : "
        f"duplicacy {' '.join(init_args[1:])}"
    )

    with tempfile.TemporaryDirectory() as scratch_dir:
        returncode = run_duplicacy(
            init_args,
            Path(scratch_dir),
            run_logger,
            env=duplicacy_init_env(entry.password, entry.ssh_key_file),
        )

    if returncode != 0:
        log_error(
            run_logger,
            config,
            run_uuid,
            f"Failed to initialize destination '{entry.name}'. Duplicacy init "
            "returned a non-zero exit code",
        )
        return False

    run_logger.info(f"Destination '{entry.name}' initialized")
    return True


def preflight_destination(
    entry: StorageEntry,
    backup_directory: Path,
    config: Config,
    run_logger: RunLogger,
    run_uuid: str,
    known_hosts_path: Path,
) -> PreflightResult:
    """Validates one destination before backing up or copying to it, first
    making sure it's actually been initialized (a "chunks" directory is
    present in its storage) and running `duplicacy init` against it via
    initialize_storage if not -- this can happen if the destination was
    newly provisioned or wiped since its entry was added to preferences.
    For an sftp destination, this also tests connectivity and looks up
    whether we're allowed to modify the remote directory.

    The sftp_target is included even on failure (when it got far enough to
    exist) so the caller can still track it as a possible log-upload
    target."""
    if entry.is_local:
        storage_path = Path(entry.storage)
        if not storage_path.is_absolute():
            storage_path = backup_directory / storage_path
        if (storage_path / "chunks").is_dir():
            return PreflightResult(ok=True)
        if not initialize_storage(
            entry, str(storage_path), config, run_logger, run_uuid
        ):
            return PreflightResult(ok=False)
        return PreflightResult(ok=True, initialized=not config.dry_run)

    if not entry.storage.startswith("sftp"):
        log_error(
            run_logger,
            config,
            run_uuid,
            f"Destination '{entry.name}' has an unsupported storage type: {entry.storage}",
        )
        return PreflightResult(ok=False)

    client, server, port = parse_sftp_target(entry.storage)
    if not client or not server:
        log_error(
            run_logger,
            config,
            run_uuid,
            f"Unable to parse sftp target for destination '{entry.name}'",
        )
        return PreflightResult(ok=False)

    if not entry.ssh_key_file:
        log_error(
            run_logger,
            config,
            run_uuid,
            f"Destination '{entry.name}' is missing keys.ssh_key_file in duplicacy preferences",
        )
        return PreflightResult(ok=False)

    remote_root = derive_remote_name(entry.storage)
    sftp_target = SftpTarget(
        entry.name,
        client,
        server,
        port,
        entry.ssh_key_file,
        remote_root,
        writable=False,
    )

    if not test_sftp_connectivity(
        client, server, port, entry.ssh_key_file, known_hosts_path
    ):
        log_error(
            run_logger,
            config,
            run_uuid,
            f"Unable to connect to {client}@{server} over sftp for destination "
            f"'{entry.name}' Aborting",
        )
        return PreflightResult(ok=False, sftp_target=sftp_target)

    if not storage_initialized_over_sftp(
        client, server, port, entry.ssh_key_file, known_hosts_path, remote_root
    ):
        if not initialize_storage(entry, entry.storage, config, run_logger, run_uuid):
            return PreflightResult(ok=False, sftp_target=sftp_target)
        if config.dry_run:
            # The remote directory may not even exist yet, so there's no
            # owner to look up until init has really run
            return PreflightResult(ok=True, sftp_target=sftp_target, initialized=False)

    owner = get_remote_directory_owner(
        client, server, port, entry.ssh_key_file, known_hosts_path, remote_root
    )
    if owner is None:
        log_error(
            run_logger,
            config,
            run_uuid,
            f"Unable to determine owner of directory on server for destination '{entry.name}'",
        )
        return PreflightResult(ok=False, sftp_target=sftp_target)

    sftp_target.writable = owner != "0"
    return PreflightResult(ok=True, sftp_target=sftp_target)


def check_rsa_encryption(
    local_entry: StorageEntry, config: Config, run_logger: RunLogger, run_uuid: str
) -> EncryptionStatus:
    """Determines whether local_entry's storage is RSA-encrypted, by asking
    `duplicacy info` to decrypt and print its config (RSA always requires
    password encryption as a prerequisite, so an unencrypted entry can never
    be RSA). Returns UNKNOWN -- never a guess -- when this can't be
    determined, so the caller treats it as a hard failure rather than
    silently choosing a path."""
    if not local_entry.encrypted:
        return EncryptionStatus.NOT_RSA

    if not local_entry.password:
        log_error(
            run_logger,
            config,
            run_uuid,
            f"Destination '{local_entry.name}' is encrypted but missing keys.password in "
            "duplicacy preferences",
        )
        return EncryptionStatus.UNKNOWN

    result = subprocess.run(
        [str(config.duplicacy_binary), "-verbose", "info", "-e", local_entry.storage],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "DUPLICACY_PASSWORD": local_entry.password},
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        log_error(
            run_logger,
            config,
            run_uuid,
            f"Unable to determine encryption details for destination '{local_entry.name}': "
            f"duplicacy info exited {result.returncode}",
        )
        return EncryptionStatus.UNKNOWN

    is_rsa = "RSA public key:" in result.stdout + result.stderr
    return EncryptionStatus.RSA if is_rsa else EncryptionStatus.NOT_RSA


def run_backup_to_destination(
    entry: StorageEntry,
    backup_directory: Path,
    config: Config,
    run_logger: RunLogger,
    run_uuid: str,
    kbyte_limit: int | None,
) -> bool:
    """Runs `duplicacy backup -storage <entry.name>` (with -dry-run in a dry
    run), logging and pinging healthchecks.io on failure. Returns whether it
    succeeded."""
    backup_args = [
        str(config.duplicacy_binary),
        "-log",
        "backup",
        "-stats",
        "-storage",
        entry.name,
    ]
    if config.dry_run:
        backup_args.append("-dry-run")
    if kbyte_limit is not None:
        backup_args += ["-limit-rate", str(kbyte_limit)]

    run_logger.info(
        f"Beginning backup of {backup_directory} to destination '{entry.name}' : "
        f"duplicacy {' '.join(backup_args[1:])}"
    )
    returncode = run_duplicacy(backup_args, backup_directory, run_logger)
    if returncode != 0:
        log_error(
            run_logger,
            config,
            run_uuid,
            f"Backup of {backup_directory} to destination '{entry.name}' failed. "
            "Duplicacy returned a non-zero exit-code",
        )
        return False

    run_logger.info(
        f"Backup of {backup_directory} to destination '{entry.name}' succeeded"
    )
    return True


def run_copy_to_destination(
    entry: StorageEntry,
    source_name: str,
    backup_directory: Path,
    config: Config,
    run_logger: RunLogger,
    run_uuid: str,
    kbyte_limit: int | None,
) -> bool:
    """Runs `duplicacy copy -from source_name -to entry.name`, logging and
    pinging healthchecks.io on failure. Returns whether it succeeded.
    `duplicacy copy` has no -dry-run option, so a dry run only logs the
    command."""
    copy_args = [
        str(config.duplicacy_binary),
        "-log",
        "copy",
        "-from",
        source_name,
        "-to",
        entry.name,
    ]
    if kbyte_limit is not None:
        copy_args += [
            "-upload-limit-rate",
            str(kbyte_limit),
            "-download-limit-rate",
            str(kbyte_limit),
        ]

    if config.dry_run:
        run_logger.info(
            f"Dry run: would copy {backup_directory} from '{source_name}' to "
            f"destination '{entry.name}' : duplicacy {' '.join(copy_args[1:])}"
        )
        return True

    run_logger.info(
        f"Beginning copy of {backup_directory} from '{source_name}' to destination "
        f"'{entry.name}' : duplicacy {' '.join(copy_args[1:])}"
    )
    returncode = run_duplicacy(copy_args, backup_directory, run_logger)
    if returncode != 0:
        log_error(
            run_logger,
            config,
            run_uuid,
            f"Copy of {backup_directory} from '{source_name}' to destination '{entry.name}' "
            "failed. Duplicacy returned a non-zero exit-code",
        )
        return False

    run_logger.info(
        f"Copy of {backup_directory} from '{source_name}' to destination '{entry.name}' "
        "succeeded"
    )
    return True


def duplicacy_prune_destination(
    entry: StorageEntry,
    backup_directory: Path,
    config: Config,
    run_logger: RunLogger,
    run_uuid: str,
) -> None:
    """Runs `duplicacy prune -storage <entry.name>` with the standard
    retention schedule (with -dry-run in a dry run), logging and pinging
    healthchecks.io on failure."""
    prune_args = [
        str(config.duplicacy_binary),
        "-log",
        "prune",
        "-storage",
        entry.name,
        *PRUNE_KEEP_ARGS,
    ]
    if config.dry_run:
        prune_args.append("-dry-run")
    run_logger.info(
        f"Beginning prune of destination '{entry.name}' : duplicacy {' '.join(prune_args[1:])}"
    )
    returncode = run_duplicacy(prune_args, backup_directory, run_logger)
    if returncode != 0:
        log_error(
            run_logger,
            config,
            run_uuid,
            f"Duplicacy prune of destination '{entry.name}' returned a non-zero exit code",
        )
    else:
        run_logger.info(f"Prune of destination '{entry.name}' succeeded")


def process_destination(
    entry: StorageEntry,
    copy_source: str | None,
    backup_directory: Path,
    config: Config,
    run_logger: RunLogger,
    run_uuid: str,
    known_hosts_path: Path,
    kbyte_limit: int | None,
) -> Outcome:
    """Validates, then backs up (copy_source is None) or copies (copy_source
    names the source destination) to one destination, and prunes it
    afterward if that succeeded and we're allowed to."""
    preflight = preflight_destination(
        entry, backup_directory, config, run_logger, run_uuid, known_hosts_path
    )
    if not preflight.ok:
        return Outcome(ok=False, sftp_target=preflight.sftp_target)

    if not preflight.initialized:
        run_logger.info(
            f"Dry run: skipping backup/copy and prune of destination '{entry.name}' "
            "since its storage hasn't been initialized"
        )
        return Outcome(ok=True, sftp_target=preflight.sftp_target)

    if copy_source is not None:
        step_ok = run_copy_to_destination(
            entry,
            copy_source,
            backup_directory,
            config,
            run_logger,
            run_uuid,
            kbyte_limit,
        )
    else:
        step_ok = run_backup_to_destination(
            entry, backup_directory, config, run_logger, run_uuid, kbyte_limit
        )
    if not step_ok:
        return Outcome(ok=False, sftp_target=preflight.sftp_target)

    if can_modify_destination(preflight.sftp_target):
        duplicacy_prune_destination(
            entry, backup_directory, config, run_logger, run_uuid
        )
    else:
        run_logger.info(
            f"Skipping prune of {backup_directory} destination '{entry.name}' as data on "
            "server is owned by root"
        )

    return Outcome(ok=True, sftp_target=preflight.sftp_target)


def plan_destinations(
    entries: list[StorageEntry], config: Config, run_logger: RunLogger, run_uuid: str
) -> list[PlannedDestination] | None:
    """Decides, for one backup_directory's destinations, whether each gets a
    real backup or a cheap copy from the local destination. Returns the
    steps in the order they should be processed, or None if a hard failure
    means nothing should be attempted (see check_rsa_encryption's
    docstring: we never silently guess)."""
    local_entry = find_local_entry(entries)

    if local_entry is None or len(entries) <= 1:
        return [PlannedDestination(entry) for entry in entries]

    rsa_status = check_rsa_encryption(local_entry, config, run_logger, run_uuid)
    if rsa_status is EncryptionStatus.UNKNOWN:
        return None

    if rsa_status is EncryptionStatus.RSA:
        run_logger.info(
            f"Destination '{local_entry.name}' is RSA-encrypted; backing up to every "
            "destination independently"
        )
        return [PlannedDestination(entry) for entry in entries]

    run_logger.info(
        f"Destination '{local_entry.name}' is not RSA-encrypted; backing up to it directly "
        "and copying to the other destinations"
    )
    return [PlannedDestination(local_entry)] + [
        PlannedDestination(entry, copy_source=local_entry.name)
        for entry in entries
        if entry is not local_entry
    ]


def run_backup_directory(
    backup_directory: Path,
    config: Config,
    run_logger: RunLogger,
    run_uuid: str,
    known_hosts_path: Path,
    kbyte_limit: int | None,
) -> Outcome:
    """Backs up (or copies, per plan_destinations) every destination
    configured for backup_directory, pruning each as permitted. A failure on
    one destination doesn't stop the others; the returned Outcome reflects
    whether everything succeeded."""
    if not backup_directory.is_dir():
        log_error(
            run_logger,
            config,
            run_uuid,
            f"Backup directory {backup_directory} does not exist",
        )
        return Outcome(ok=False)

    if is_empty_backup_directory(backup_directory):
        log_error(
            run_logger,
            config,
            run_uuid,
            f"Backup directory {backup_directory} is empty; add symlinks for the paths "
            "you want backed up (see `duplicacy-backup-runner setup`) Aborting",
        )
        return Outcome(ok=False)

    if has_non_symlink_directories(backup_directory):
        log_error(
            run_logger,
            config,
            run_uuid,
            f"Backup directory {backup_directory} contains non-symlinked directories which will "
            "fail to backup symlinks within them Aborting",
        )
        return Outcome(ok=False)

    entries = read_storage_entries(backup_directory)

    log_directory_contents(run_logger, backup_directory)
    log_filters(run_logger, backup_directory)

    plan = plan_destinations(entries, config, run_logger, run_uuid)
    if plan is None:
        return Outcome(ok=False)

    overall_ok = True
    last_sftp_target: SftpTarget | None = None
    for planned in plan:
        outcome = process_destination(
            planned.entry,
            planned.copy_source,
            backup_directory,
            config,
            run_logger,
            run_uuid,
            known_hosts_path,
            kbyte_limit,
        )
        overall_ok = overall_ok and outcome.ok
        if outcome.sftp_target is not None:
            last_sftp_target = outcome.sftp_target

    return Outcome(ok=overall_ok, sftp_target=last_sftp_target)


def upload_log(
    run_log_path: Path,
    log_file_name: str,
    sftp_target: SftpTarget,
    known_hosts_path: Path,
    hostname_short: str,
    run_logger: RunLogger,
) -> None:
    """Uploads run_log_path to sftp_target's remote logs directory, and --
    if we're allowed to modify that destination -- repoints its "latest
    log" symlink at it. Leaves the local file in place on failure."""
    remote_logs_dir = f"{sftp_target.remote_root}/logs"
    upload_command = f'put "{run_log_path}" {remote_logs_dir}/'
    if can_modify_destination(sftp_target):
        latest_name = f"{remote_logs_dir}/duplicacy.{hostname_short}.latest.txt"
        upload_command += (
            f'\nrm "{latest_name}"\nsymlink "{log_file_name}" "{latest_name}"'
        )

    result = sftp_run(
        upload_command + "\n",
        sftp_target.client,
        sftp_target.server,
        sftp_target.port,
        sftp_target.key_file,
        known_hosts_path,
        # A long backup's run log can be many megabytes
        timeout=600,
    )
    if result.returncode == 0:
        run_logger.close_log_file(run_log_path)
        run_log_path.unlink(missing_ok=True)
    else:
        run_logger.error(
            f"Failed to upload log to {sftp_target.client}@{sftp_target.server}: {result.stderr}"
        )


@contextlib.contextmanager
def held_lock(lock_file: Path) -> Iterator[bool]:
    """Context manager around a non-blocking flock (msvcrt.locking of the
    first byte on Windows) on lock_file, so overlapping scheduled runs skip
    each other instead of running concurrently. Yields True if the lock was
    acquired -- and holds it for as long as the `with` block runs -- or
    False if another run already holds it."""
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "w") as handle:
        try:
            if IS_WINDOWS:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            if IS_WINDOWS:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a scheduled Duplicacy backup.")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Path to config.yaml (default: {default_config_path()})",
    )
    parser.add_argument(
        "--log-level", default=None, help="Console log level (overrides config)"
    )
    parser.add_argument(
        "--log-basedir",
        type=Path,
        default=None,
        help="Directory to write logs to (overrides config)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Shorthand for --log-level DEBUG"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what a run would do without changing anything: duplicacy "
        "backup/prune run with -dry-run; init, copy, provisioning, log files, "
        "log upload and healthchecks.io pings are only logged",
    )

    subparsers = parser.add_subparsers(dest="command")
    setup_parser = subparsers.add_parser(
        "setup",
        help="Interactively provision this host (SSH keys, duplicacy init, "
        "config.yaml, scheduled run)",
    )
    setup_parser.add_argument(
        "--duplicacy-basedir", type=Path, default=DEFAULT_DUPLICACY_BASEDIR
    )
    setup_parser.add_argument("--backup-directory", type=Path, default=None)
    setup_parser.add_argument("--filters-url", default=None)
    setup_parser.add_argument("--duplicacy-version", default=None)

    return parser.parse_args(argv)


def tail_of_file(path: Path, count: int) -> str:
    """Return the last `count` lines of `path` without loading the whole
    file into memory, since a multi-hour backup's run log can be large."""
    last_lines: collections.deque[str] = collections.deque(maxlen=count)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            last_lines.append(line.rstrip("\n"))
    return "\n".join(last_lines)


def create_log_files(
    log_basedir: Path, client_individual_id: str, log_file_name: str
) -> tuple[Path, Path, Path]:
    """Creates (chmod 640) this run's log file, the persistent per-client
    log, and an emptied lastrun log under log_basedir.

    :returns: The run, persistent and lastrun log paths, in RunLogger's
        argument order.
    """
    log_basedir.mkdir(parents=True, exist_ok=True)

    lastrun_log_path = log_basedir / f"duplicacy.{client_individual_id}.lastrun.txt"
    lastrun_log_path.write_text("", encoding="utf-8")
    lastrun_log_path.chmod(0o640)

    run_log_path = log_basedir / log_file_name
    run_log_path.touch()
    run_log_path.chmod(0o640)

    persistent_log_path = log_basedir / f"duplicacy.{client_individual_id}.txt"
    persistent_log_path.touch()
    persistent_log_path.chmod(0o640)

    return run_log_path, persistent_log_path, lastrun_log_path


def _run(
    config: Config,
    args: argparse.Namespace,
    hostname_short: str,
    run_uuid: str,
    console_level: int,
    dry_run_dir: Path | None = None,
) -> int:
    """Runs one full backup: sets up this run's log files, backs up every
    configured backup_directory, uploads the run log, and reports
    start/log/success/failure to healthchecks.io throughout. Returns the
    process exit code (0 if every destination in every backup_directory
    succeeded, 1 otherwise).

    In a dry run, logging goes to the console only, the log upload and
    healthchecks.io pings are skipped, and dry_run_dir is a scratch
    directory for files that would otherwise be written in place (see
    ensure_known_hosts)."""
    log_basedir = args.log_basedir or config.log_basedir
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")  # noqa: DTZ005 -- local time, matching `date`
    log_file_name = f"duplicacy.{hostname_short}.{timestamp}.txt"

    if config.dry_run:
        run_log_path = None
        run_logger = RunLogger(None, None, None, console_level)
    else:
        log_paths = create_log_files(
            log_basedir, config.client_individual_id, log_file_name
        )
        run_log_path = log_paths[0]
        run_logger = RunLogger(*log_paths, console_level)
    run_logger.info(f"Beginning run-scheduled-duplicacy-backup version {__version__}")
    if config.dry_run:
        run_logger.info(
            "Dry run: nothing will be changed. duplicacy backup and prune run with "
            "-dry-run; init, copy, provisioning, log files, log upload and "
            "healthchecks.io pings are only logged"
        )

    wait_for_internet(
        run_logger,
        config.internet_check_attempts,
        config.internet_check_delay,
        config.internet_check_url,
    )

    provision(config, run_logger)

    if config.dry_run and not config.duplicacy_binary.exists():
        run_logger.error(
            f"Dry run: can't continue without the duplicacy binary at "
            f"{config.duplicacy_binary}, which a real run would download"
        )
        return 1

    kbyte_limit = resolve_rate_limit(config.rate_limit_ip, config.rate_limit_rate)
    known_hosts_path = ensure_known_hosts(
        config.duplicacy_basedir, config.known_hosts_string, dry_run_dir
    )

    notify_healthchecks(
        config,
        run_uuid,
        "/start",
        data=f"Duplicacy backup starting (version {__version__})",
    )

    run_failed = False
    last_sftp_target: SftpTarget | None = None

    for backup_directory in config.backup_directories:
        outcome = run_backup_directory(
            backup_directory,
            config,
            run_logger,
            run_uuid,
            known_hosts_path,
            kbyte_limit,
        )
        if not outcome.ok:
            run_failed = True
        if outcome.sftp_target is not None:
            last_sftp_target = outcome.sftp_target

    wait_for_internet(
        run_logger,
        config.internet_check_attempts,
        config.internet_check_delay,
        config.internet_check_url,
    )

    if run_log_path is not None:
        notify_healthchecks(
            config, run_uuid, "/log", data=tail_of_file(run_log_path, 13)
        )

    if last_sftp_target is not None and config.dry_run:
        run_logger.info(
            f"Dry run complete, would have uploaded log to {last_sftp_target.client}@"
            f"{last_sftp_target.server}"
        )
    elif last_sftp_target is not None:
        assert run_log_path is not None
        upload_log(
            run_log_path,
            log_file_name,
            last_sftp_target,
            known_hosts_path,
            hostname_short,
            run_logger,
        )
        run_logger.info("Run complete, log uploaded to the server")
    else:
        run_logger.info("Run complete")

    if not run_failed:
        notify_healthchecks(
            config,
            run_uuid,
            "",
            data=f"Duplicacy backup completed successfully (version {__version__})",
        )

    return 1 if run_failed else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: loads config, takes the run lock, and executes one
    backup run. Returns the process exit code (0 success; 1 on failure or
    if another run already holds the lock)."""
    args = parse_args(argv)
    if IS_WINDOWS and hasattr(sys.stdout, "reconfigure"):
        # Under Task Scheduler stdout is redirected with the ANSI code page,
        # which can't encode every file name duplicacy prints
        sys.stdout.reconfigure(errors="replace")
    if args.command == "setup":
        if args.dry_run:
            raise SystemExit("--dry-run isn't supported with setup")
        from duplicacy_backup_runner.setup import cmd_setup

        return cmd_setup(args)

    config = load_config(args.config)
    config.dry_run = args.dry_run
    level_name = args.log_level or config.log_level
    console_level = (
        logging.DEBUG
        if args.verbose
        else getattr(logging, level_name.upper(), logging.INFO)
    )
    hostname_short = short_hostname()
    run_uuid = str(uuid.uuid4())

    if config.dry_run:
        # No lock: a dry run changes nothing, so it can't conflict with a real
        # run, and a long one shouldn't make a scheduled run skip
        with tempfile.TemporaryDirectory() as dry_run_dir:
            return _run(
                config, args, hostname_short, run_uuid, console_level, Path(dry_run_dir)
            )

    with held_lock(config.lock_file) as acquired:
        if not acquired:
            return 1
        return _run(config, args, hostname_short, run_uuid, console_level)


if __name__ == "__main__":
    sys.exit(main())
