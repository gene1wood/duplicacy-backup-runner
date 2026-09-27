"""Interactive `duplicacy-backup-runner setup` subcommand.

Split out of main.py once it grew past this project's ~1000-line
single-module guideline. Reaches back into main.py (via the `main` module
object, not `from ... import name`, and with `from __future__ import
annotations` below) for shared pieces -- Config/RunLogger, provision_basedir/
provision_duplicacy_binary, download_file, find_rsa_public_keys,
read_storage_entries-adjacent helpers, run_duplicacy, ensure_known_hosts,
sftp_run/parse_sftp_target/derive_remote_name, is_local_storage,
default_config_path, DEFAULT_DUPLICACY_VERSION -- since main.py in turn
dispatches to cmd_setup() here via a deferred import inside main(), avoiding
a circular import at module load time.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

import click
import yaml

from duplicacy_backup_runner import main

WINDOWS_TASK_NAME = "duplicacy-backup-runner"
# Well-known SIDs, used instead of account names, which are localized
WINDOWS_SYSTEM_SID = "*S-1-5-18"
WINDOWS_ADMINISTRATORS_SID = "*S-1-5-32-544"
WINDOWS_BROAD_SIDS = ("*S-1-1-0", "*S-1-5-11", "*S-1-5-32-545")  # Everyone,
# Authenticated Users, Users


def ask(prompt_text: str, **kwargs) -> str:
    return click.prompt(prompt_text, **kwargs)


def ask_multiline(prompt_text: str) -> str:
    """Reads lines until a blank line (or EOF), so multi-line pastes like
    ssh-keyscan output aren't cut off at the first newline -- with the rest
    leaking into the prompts that follow, as click.prompt would. Re-prompts
    until at least one non-blank line is entered.

    :returns: The entered lines joined with newlines, with a trailing
        newline.
    """
    while True:
        click.echo(f"{prompt_text} (end with a blank line):")
        lines: list[str] = []
        while True:
            line = sys.stdin.readline()
            if not line.strip():
                break
            # Trailing whitespace would stop write_config from emitting
            # known_hosts_string as a `|` literal block
            lines.append(line.rstrip())
        if lines:
            return "\n".join(lines) + "\n"
        if not line:
            raise click.Abort()


def confirm(prompt_text: str, **kwargs) -> bool:
    return click.confirm(prompt_text, **kwargs)


def announce(message: str, fg: str = "green") -> None:
    click.secho(message, fg=fg)


def announce_error(message: str) -> None:
    click.secho(f"ERROR: {message}", fg="red", err=True)


class ConsoleAnnouncer:
    """A minimal RunLogger-compatible sink for setup's interactive flow,
    which has no per-run log files -- messages just go to the console. Lets
    provision_basedir/provision_duplicacy_binary/run_duplicacy be reused
    as-is inside setup."""

    def info(self, message: str) -> None:
        announce(message)

    def error(self, message: str) -> None:
        announce_error(message)

    def debug(self, message: str) -> None:
        pass

    def write_raw(self, line: str) -> None:
        click.echo(line)


def check_setup_prerequisites() -> list[str]:
    """Returns the names of any required external commands not found on
    PATH. openssl is only needed for RSA encryption, which isn't offered on
    Windows unless openssl happens to be installed (see
    prompt_encryption_choice)."""
    missing = [] if shutil.which(main.sftp_executable()) else ["sftp"]
    if not main.IS_WINDOWS and shutil.which("openssl") is None:
        missing.append("openssl")
    return missing


def prompt_for_backup_directory() -> Path:
    return Path(ask("Backup directory to provision", type=click.Path()))


def prompt_until_symlinks_present(backup_directory: Path) -> None:
    """Waits for the user to populate backup_directory with symlinks to the
    paths they want backed up."""
    example = (
        f"From an administrator command prompt: mklink /D "
        f"{backup_directory / 'C-Users'} C:\\Users"
        if main.IS_WINDOWS
        else f"ln --verbose --symbolic /boot {backup_directory}/boot"
    )
    while not any(
        entry for entry in backup_directory.iterdir() if entry.name != ".duplicacy"
    ):
        click.secho(
            f"You'll need to set up symbolic links in {backup_directory}\n"
            f"Example: {example}",
            fg="yellow",
        )
        ask("Press enter once you've done this", default="", show_default=False)


def prompt_for_storage_url(client: str) -> str:
    """Prompts for either a remote sftp server or a local destination
    directory, returning the resulting storage URL."""
    if confirm("Back up to a remote sftp server", default=True):
        server = ask("SFTP server hostname")
        port = ask("SFTP server port", default=22, type=int)
        return f"sftp://{client}@{server}:{port}/backup"
    return ask("Local destination directory", type=click.Path())


def prompt_for_ssh_key_file(client: str, keys_dir: Path) -> Path:
    """Prompts for the path to the SSH private key copied over from the
    server for this client, defaulting to the first id_*_<client> match in
    keys_dir, and restricts its permissions with secure_key_file."""
    candidates = sorted(keys_dir.glob(f"id_*_{client}"))
    default = str(candidates[0]) if candidates else None
    key_file = Path(
        ask(
            "Path to this client's SSH private key",
            default=default,
            type=click.Path(exists=True),
        )
    )
    secure_key_file(key_file)
    return key_file


def secure_key_file(key_file: Path) -> None:
    """Restricts key_file's permissions so ssh will use it: chmod 600 on
    POSIX. Windows OpenSSH instead rejects a key whose owner isn't SYSTEM,
    Administrators or the current user, or whose ACL grants anyone else
    access, so there this makes Administrators the owner and grants access
    only to SYSTEM (which the scheduled task runs as) and Administrators
    (for setup, run from an elevated prompt). Ownership is taken first,
    with takeown since icacls /setowner needs access the key may not grant,
    so this also works on a key that's accessible only to SYSTEM."""
    if not main.IS_WINDOWS:
        key_file.chmod(0o600)
        return
    subprocess.run(["takeown", "/F", str(key_file), "/A"], check=True)
    icacls = ["icacls", str(key_file)]
    subprocess.run([*icacls, "/inheritance:r"], check=True)
    # Not checked, since the current user may have no entry to remove
    subprocess.run(
        [*icacls, "/remove:g", *WINDOWS_BROAD_SIDS, getpass.getuser()], check=False
    )
    subprocess.run(
        [
            *icacls,
            "/grant:r",
            f"{WINDOWS_SYSTEM_SID}:F",
            f"{WINDOWS_ADMINISTRATORS_SID}:F",
        ],
        check=True,
    )


def prompt_for_known_hosts_string() -> str:
    return ask_multiline("known_hosts entries for the server (ssh-keyscan output)")


def openssl_is_version_3() -> bool:
    result = subprocess.run(
        ["openssl", "version"], capture_output=True, text=True, check=False
    )
    return "OpenSSL 3" in result.stdout


def generate_rsa_keypair(keys_dir: Path, client: str) -> Path:
    """Generates an RSA keypair for async encryption under keys_dir, using
    -traditional on OpenSSL 3 (see
    https://forum.duplicacy.com/t/duplicacy-restore-of-encrypted-backup-fails-with-error/3862/14),
    and returns the public key's path."""
    private_key = keys_dir / f"{client}_duplicacy_encryption_key_private.pem"
    public_key = keys_dir / f"{client}_duplicacy_encryption_key_public.pem"
    traditional_args = ["-traditional"] if openssl_is_version_3() else []
    subprocess.run(
        [
            "openssl",
            "genrsa",
            "-aes256",
            *traditional_args,
            "-out",
            str(private_key),
            "2048",
        ],
        check=True,
    )
    subprocess.run(
        ["openssl", "rsa", "-in", str(private_key), "-pubout", "-out", str(public_key)],
        check=True,
    )
    return public_key


def prompt_encryption_choice(client: str, keys_dir: Path) -> tuple[bool, Path | None]:
    """Asks sync (password) vs async (RSA) encryption. For RSA, reuses
    find_rsa_public_keys (content-based, same detection initialize_storage
    uses) to find an existing key for this host before generating a new
    one, and refuses to guess if more than one candidate is already
    present."""
    if not confirm("Encrypt this backup", default=True):
        return False, None
    choice = ask(
        "Synchronous (password) or asynchronous (RSA) encryption",
        type=click.Choice(["sync", "async"]),
        default="sync",
    )
    if choice == "sync":
        return True, None
    if shutil.which("openssl") is None:
        # Only possible on Windows; check_setup_prerequisites requires it
        # elsewhere
        announce(
            "openssl isn't installed, so RSA encryption isn't available; using "
            "password encryption",
            fg="yellow",
        )
        return True, None
    candidates = main.find_rsa_public_keys(keys_dir)
    if len(candidates) > 1:
        announce_error(
            f"Found multiple candidate RSA public keys in {keys_dir}; refusing to guess"
        )
        raise SystemExit(1)
    if candidates:
        return True, candidates[0]
    return True, generate_rsa_keypair(keys_dir, client)


def prompt_storage_password(encrypted: bool) -> str | None:
    if not encrypted:
        return None
    return ask("Storage encryption password", hide_input=True, confirmation_prompt=True)


def initialize_repository(
    duplicacy_binary: Path,
    backup_directory: Path,
    client: str,
    storage_url: str,
    encrypted: bool,
    rsa_public_key: Path | None,
    password: str | None,
    ssh_key_file: Path | None,
    run_logger: main.RunLogger,
) -> bool:
    """Runs `duplicacy init -repository backup_directory` against storage_url
    for the first time, creating .duplicacy/preferences locally in the
    process -- unlike initialize_storage, which only re-initializes an
    already-registered destination's remote storage from a scratch
    directory. The password is passed via DUPLICACY_PASSWORD, non-
    interactively, the same pattern initialize_storage already uses,
    rather than letting duplicacy prompt at the TTY -- the caller already
    has the password as a Python value (to populate preferences via
    duplicacy_set afterward), so there's no reason to make the user type it
    twice. ssh_key_file is likewise passed via DUPLICACY_SSH_KEY_FILE, as
    there's no preferences file yet for duplicacy to find it in."""
    init_args = [
        str(duplicacy_binary),
        "-log",
        "init",
        "-repository",
        str(backup_directory),
    ]
    if encrypted:
        init_args.append("-e")
    if rsa_public_key is not None:
        init_args += ["-key", str(rsa_public_key)]
    init_args += [client, storage_url]

    announce(f"Initializing repository: duplicacy {' '.join(init_args[1:])}", fg="cyan")
    returncode = main.run_duplicacy(
        init_args,
        backup_directory,
        run_logger,
        env=main.duplicacy_init_env(password, ssh_key_file),
    )
    if returncode != 0:
        announce_error("duplicacy init failed")
        return False
    return True


def create_destination_logs_directory(
    storage_url: str, ssh_key_file: Path | None, known_hosts_path: Path
) -> None:
    """Creates a logs/ directory at the destination -- locally, or over sftp
    (mkdir is harmlessly a no-op if it already exists)."""
    if main.is_local_storage(storage_url):
        (Path(storage_url) / "logs").mkdir(parents=True, exist_ok=True)
        return
    client, server, port = main.parse_sftp_target(storage_url)
    remote_root = main.derive_remote_name(storage_url)
    main.sftp_run(
        f"mkdir {remote_root}/logs\n",
        client,
        server,
        port,
        ssh_key_file,
        known_hosts_path,
    )


def prompt_and_fetch_filters(backup_directory: Path, filters_url: str | None) -> None:
    """Downloads a default .duplicacy/filters file if one isn't already
    present, from filters_url or a URL the user is prompted for (blank to
    skip)."""
    filters_path = backup_directory / ".duplicacy" / "filters"
    if filters_path.exists():
        return
    url = filters_url or ask(
        "URL to fetch a default .duplicacy/filters from (blank to skip)",
        default="",
        show_default=False,
    )
    if not url:
        return
    main.download_file(url, filters_path)
    announce(f"Fetched filters to {filters_path}")


def read_existing_default_entry(backup_directory: Path) -> main.StorageEntry | None:
    """Returns the first ("default") destination from backup_directory's
    existing .duplicacy/preferences, or None if the repository hasn't been
    initialized yet. An unreadable preferences file is fatal rather than
    silently re-initialized over.

    :param backup_directory: The repository to look in.
    :type backup_directory: Path
    :returns: The default destination entry, or None if there's no
        preferences file.
    :rtype: main.StorageEntry | None
    """
    preferences_path = backup_directory / ".duplicacy" / "preferences"
    if not preferences_path.is_file():
        return None
    try:
        entries = main.read_storage_entries(backup_directory)
    except (OSError, ValueError, KeyError) as error:
        announce_error(f"Unable to read {preferences_path}: {error}")
        raise SystemExit(1)
    if not entries:
        announce_error(f"{preferences_path} contains no destinations")
        raise SystemExit(1)
    return entries[0]


def duplicacy_set(
    duplicacy_binary: Path,
    backup_directory: Path,
    key: str,
    value: str,
    storage_name: str | None = None,
) -> None:
    """Runs `duplicacy set -key <key> -value <value>` against
    backup_directory's preferences, optionally scoped to one storage -- this
    is how ssh_key_file and password entries get into
    .duplicacy/preferences."""
    args = [str(duplicacy_binary), "set", "-key", key, "-value", value]
    if storage_name is not None:
        args += ["-storage", storage_name]
    result = subprocess.run(
        args,
        cwd=backup_directory,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        announce_error(f"duplicacy set -key {key} failed: {result.stderr}")
        raise SystemExit(1)


def load_existing_config_dict(config_path: Path) -> dict:
    try:
        return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}


def prompt_for_healthchecks_uuid(existing: str | None = None) -> str:
    while True:
        value = ask("healthchecks.io check UUID", default=existing)
        try:
            uuid.UUID(value)
        except ValueError:
            announce_error("That doesn't look like a UUID")
            continue
        return value


def merge_backup_directory_into_config(
    raw_config: dict, backup_directory: Path
) -> dict:
    """Adds backup_directory to raw_config's backup_directories list if it's
    not already there, rather than clobbering an existing setup."""
    directories = raw_config.setdefault("backup_directories", [])
    if all(Path(existing) != backup_directory for existing in directories):
        directories.append(str(backup_directory))
    return raw_config


class ConfigDumper(yaml.SafeDumper):
    """Writes multi-line strings (like known_hosts_string) as `|` literal
    blocks instead of quoted strings with escaped or doubled newlines."""


def represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


ConfigDumper.add_representer(str, represent_str)


def write_config(config_path: Path, raw_config: dict) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump(raw_config, Dumper=ConfigDumper), encoding="utf-8")


def prompt_and_write_config(
    config_path: Path,
    backup_directory: Path,
    duplicacy_basedir: Path,
    known_hosts_string: str | None,
    filters_url: str | None,
) -> dict:
    raw_config = load_existing_config_dict(config_path)
    raw_config["healthchecks_uuid"] = prompt_for_healthchecks_uuid(
        raw_config.get("healthchecks_uuid")
    )
    raw_config.setdefault("client_individual_id", main.short_hostname())
    if duplicacy_basedir != main.DEFAULT_DUPLICACY_BASEDIR:
        raw_config["duplicacy_basedir"] = str(duplicacy_basedir)
    if known_hosts_string:
        raw_config["known_hosts_string"] = known_hosts_string
    if filters_url:
        raw_config["filters_url"] = filters_url
    raw_config = merge_backup_directory_into_config(raw_config, backup_directory)
    write_config(config_path, raw_config)
    announce(f"Wrote {config_path}")
    return raw_config


def running_as_root() -> bool:
    """True if running as root, or on Windows, as an elevated
    administrator."""
    if main.IS_WINDOWS:
        import ctypes

        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    return os.geteuid() == 0


def detect_init_system() -> str:
    result = subprocess.run(
        ["ps", "--no-headers", "-o", "comm", "1"],
        capture_output=True,
        text=True,
        check=False,
    )
    return "systemd" if result.stdout.strip() == "systemd" else "cron"


def resolve_console_script_path() -> str:
    return (
        shutil.which("duplicacy-backup-runner")
        or f"{sys.executable} -m duplicacy_backup_runner.main"
    )


def build_exec_command(duplicacy_backup_runner_path: str, config_path: Path) -> str:
    command = duplicacy_backup_runner_path
    if config_path != main.default_config_path():
        command += f" --config {config_path}"
    return command


def systemd_service_unit_content(exec_command: str) -> str:
    return (
        "[Unit]\n"
        "Description=Incremental backup with Duplicacy followed by pruning\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={exec_command}\n"
    )


def systemd_timer_unit_content() -> str:
    return (
        "[Unit]\n"
        "Description=Run duplicacy-backup-runner every night between 1AM and 4AM PST\n"
        "\n"
        "[Timer]\n"
        "OnCalendar=*-*-* 09:00:00 UTC\n"
        "# triggers the service immediately if it missed the last start time\n"
        "Persistent=true\n"
        "# Delay between 0 and 3 hours in seconds (3 * 60 * 60 = 10800)\n"
        "RandomizedDelaySec=10800\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def cron_d_content(exec_command: str) -> str:
    return (
        "SHELL=/bin/bash\n"
        "# Run duplicacy-backup-runner every night between 1AM and 4AM\n"
        "# Delay between 0 and 3 hours in seconds (3 * 60 * 60 = 10800)\n"
        f"0 1 * * * root sleep $[RANDOM % 10800]; {exec_command}\n"
    )


def install_systemd_units(
    exec_command: str, unit_dir: Path = Path("/etc/systemd/system")
) -> None:
    """Writes the service/timer units if missing, only enabling the timer
    when it was just written."""
    service_path = unit_dir / "duplicacy-backup-runner.service"
    timer_path = unit_dir / "duplicacy-backup-runner.timer"
    if not service_path.exists():
        service_path.write_text(systemd_service_unit_content(exec_command))
    if not timer_path.exists():
        timer_path.write_text(systemd_timer_unit_content())
        subprocess.run(
            ["systemctl", "enable", "duplicacy-backup-runner.timer", "--now"],
            check=False,
        )


def install_cron_d_entry(
    exec_command: str,
    cron_path: Path = Path("/etc/cron.d/duplicacy-backup-runner.cron"),
) -> None:
    if not cron_path.exists():
        cron_path.write_text(cron_d_content(exec_command))


def print_would_install_scheduled_run(exec_command: str) -> None:
    click.secho(
        "Not running as root -- nothing installed. Run as root to install a "
        "scheduled run, or copy the content below yourself:",
        fg="yellow",
    )
    if detect_init_system() == "systemd":
        click.echo(systemd_service_unit_content(exec_command))
        click.echo(systemd_timer_unit_content())
    else:
        click.echo(cron_d_content(exec_command))


def install_scheduled_run(exec_command: str) -> None:
    if not running_as_root():
        print_would_install_scheduled_run(exec_command)
        return
    if detect_init_system() == "systemd":
        install_systemd_units(exec_command)
    else:
        install_cron_d_entry(exec_command)


def windows_task_action(config_path: Path) -> tuple[str, str]:
    """The Command and Arguments for the Windows scheduled task, which Task
    Scheduler keeps separate (unlike the single command line that
    build_exec_command produces for systemd and cron), so a path containing
    spaces needs no quoting in Command.

    :returns: The executable and its command-line-quoted arguments.
    """
    script = shutil.which("duplicacy-backup-runner")
    if script:
        command, arguments = script, []
    else:
        command, arguments = sys.executable, ["-m", "duplicacy_backup_runner.main"]
    if config_path != main.default_config_path():
        arguments += ["--config", str(config_path)]
    return command, subprocess.list2cmdline(arguments)


def windows_task_xml(command: str, arguments: str, working_directory: Path) -> str:
    """Task Scheduler XML equivalent to the systemd timer: nightly between
    1AM and 4AM local time, run as SYSTEM, started late if the machine was
    off (StartWhenAvailable, like systemd's Persistent=true), and skipped if
    a run is still going. The XML declaration names no encoding, so
    schtasks goes by the file's byte order mark."""
    arguments_element = (
        f"      <Arguments>{escape(arguments)}</Arguments>\n" if arguments else ""
    )
    return (
        '<?xml version="1.0"?>\n'
        '<Task version="1.2" '
        'xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        "    <Description>Incremental backup with Duplicacy followed by pruning"
        "</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <CalendarTrigger>\n"
        "      <StartBoundary>2026-01-01T01:00:00</StartBoundary>\n"
        "      <RandomDelay>PT3H</RandomDelay>\n"
        "      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>\n"
        "    </CalendarTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        "      <UserId>S-1-5-18</UserId>\n"
        "      <RunLevel>HighestAvailable</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{escape(command)}</Command>\n"
        f"{arguments_element}"
        f"      <WorkingDirectory>{escape(str(working_directory))}"
        "</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def install_windows_scheduled_task(config_path: Path, working_directory: Path) -> None:
    """Registers the WINDOWS_TASK_NAME scheduled task if it doesn't already
    exist, or, when not elevated, prints its XML for the user to import."""
    command, arguments = windows_task_action(config_path)
    task_xml = windows_task_xml(command, arguments, working_directory)
    if not running_as_root():
        click.secho(
            "Not running as administrator -- nothing installed. Run setup from an "
            "elevated prompt to install a scheduled task, or save the XML below "
            f"and run: schtasks /Create /TN {WINDOWS_TASK_NAME} /XML <file>",
            fg="yellow",
        )
        click.echo(task_xml)
        return
    query = subprocess.run(
        ["schtasks", "/Query", "/TN", WINDOWS_TASK_NAME],
        capture_output=True,
        check=False,
    )
    if query.returncode == 0:
        return
    with tempfile.TemporaryDirectory() as scratch_dir:
        xml_path = Path(scratch_dir) / "task.xml"
        xml_path.write_text(task_xml, encoding="utf-16")
        subprocess.run(
            ["schtasks", "/Create", "/TN", WINDOWS_TASK_NAME, "/XML", str(xml_path)],
            check=False,
        )


def print_first_run_instructions(
    duplicacy_binary: Path, backup_directory: Path
) -> None:
    click.secho("\nSetup complete.", fg="green", bold=True)
    click.echo("To do a dry run to see what would be backed up:")
    click.echo(f"  cd {backup_directory}")
    click.echo(f"  {duplicacy_binary} backup -stats -dry-run")


def cmd_setup(args: argparse.Namespace) -> int:
    """Interactively provisions this host:
    installs the duplicacy binary, walks through SSH keys and encryption,
    initializes the repository, fetches filters, writes config.yaml, and
    installs a scheduled run. If backup_directory already has a
    .duplicacy/preferences, its default destination's storage URL,
    encryption, ssh_key_file and password are reused instead of prompted
    for (only ones missing from it are asked), and `duplicacy init` is
    skipped."""
    missing = check_setup_prerequisites()
    if missing:
        announce_error(f"Missing required executables: {', '.join(missing)}")
        return 1

    duplicacy_basedir = args.duplicacy_basedir
    main.provision_basedir(
        duplicacy_basedir, duplicacy_basedir / "logs", ConsoleAnnouncer()
    )
    main.provision_duplicacy_binary(
        main.duplicacy_binary_path(duplicacy_basedir),
        args.duplicacy_version or main.DEFAULT_DUPLICACY_VERSION,
        None,
        ConsoleAnnouncer(),
    )

    backup_directory = args.backup_directory or prompt_for_backup_directory()
    backup_directory.mkdir(parents=True, exist_ok=True)
    prompt_until_symlinks_present(backup_directory)

    client = main.short_hostname()
    duplicacy_binary = main.duplicacy_binary_path(duplicacy_basedir)
    existing = read_existing_default_entry(backup_directory)
    if existing is None:
        storage_url = prompt_for_storage_url(client)
    else:
        announce(
            f"Using existing destination '{existing.name}' ({existing.storage}) "
            f"from {backup_directory / '.duplicacy' / 'preferences'}",
            fg="cyan",
        )
        storage_url = existing.storage
        if main.is_local_storage(storage_url):
            # duplicacy resolves a relative local storage path against the
            # repository, not setup's working directory
            storage_url = str(backup_directory / storage_url)

    # Only values that weren't already in preferences get written back with
    # duplicacy set below
    new_ssh_key_file: Path | None = None
    ssh_key_file: Path | None = None
    known_hosts_string: str | None = None
    known_hosts_path = duplicacy_basedir / "keys" / "known_hosts"
    if not main.is_local_storage(storage_url):
        if existing is not None and existing.ssh_key_file is not None:
            ssh_key_file = existing.ssh_key_file
            main.provision_ssh_key_permissions(backup_directory, ConsoleAnnouncer())
            if main.IS_WINDOWS and ssh_key_file.is_file():
                secure_key_file(ssh_key_file)
        else:
            ssh_key_file = new_ssh_key_file = prompt_for_ssh_key_file(
                client, duplicacy_basedir / "keys"
            )
        known_hosts_string = prompt_for_known_hosts_string()
        known_hosts_path = main.ensure_known_hosts(
            duplicacy_basedir, known_hosts_string
        )

    new_password: str | None = None
    if existing is None:
        encrypted, rsa_public_key = prompt_encryption_choice(
            client, duplicacy_basedir / "keys"
        )
        new_password = prompt_storage_password(encrypted)
        if not initialize_repository(
            duplicacy_binary,
            backup_directory,
            client,
            storage_url,
            encrypted,
            rsa_public_key,
            new_password,
            ssh_key_file,
            ConsoleAnnouncer(),
        ):
            return 1
    elif existing.encrypted and not existing.password:
        new_password = prompt_storage_password(True)

    create_destination_logs_directory(storage_url, ssh_key_file, known_hosts_path)
    prompt_and_fetch_filters(backup_directory, args.filters_url)

    storage_name = existing.name if existing is not None else None
    if new_ssh_key_file is not None:
        duplicacy_set(
            duplicacy_binary,
            backup_directory,
            "ssh_key_file",
            str(new_ssh_key_file),
            storage_name,
        )
    if new_password is not None:
        duplicacy_set(
            duplicacy_binary, backup_directory, "password", new_password, storage_name
        )

    config_path = args.config or main.default_config_path()
    prompt_and_write_config(
        config_path,
        backup_directory,
        duplicacy_basedir,
        known_hosts_string,
        args.filters_url,
    )

    if main.IS_WINDOWS:
        install_windows_scheduled_task(config_path, duplicacy_basedir)
    else:
        exec_command = build_exec_command(resolve_console_script_path(), config_path)
        install_scheduled_run(exec_command)

    print_first_run_instructions(duplicacy_binary, backup_directory)
    return 0
