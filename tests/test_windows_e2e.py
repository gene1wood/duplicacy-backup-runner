"""End-to-end checks of the Windows-specific pieces on a real Windows host:
SSH key file ACLs as Windows OpenSSH sees them, and the Task Scheduler task
that setup registers running a real backup as SYSTEM.

These change machine-wide state (C:\\duplicacy, a scheduled task, file
ownership), so they're skipped unless DUPLICACY_BACKUP_RUNNER_WINDOWS_E2E=1,
which the "Windows tests" GitHub Actions workflow sets on its throwaway
runner. They need an elevated prompt, the built-in OpenSSH Client, and a
duplicacy.exe in DUPLICACY_TEST_BINARY.
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from duplicacy_backup_runner import main, setup

pytestmark = pytest.mark.skipif(
    sys.platform != "win32"
    or os.environ.get("DUPLICACY_BACKUP_RUNNER_WINDOWS_E2E") != "1",
    reason="Windows end-to-end tests; set DUPLICACY_BACKUP_RUNNER_WINDOWS_E2E=1 "
    "on a disposable Windows machine to run them",
)

# The built-in OpenSSH's ssh-keygen, not e.g. Git for Windows' one that may be
# first on PATH, since it's Windows OpenSSH's key permission check under test
SSH_KEYGEN = main.WINDOWS_SFTP_PATH.parent / "ssh-keygen.exe"
E2E_DIR = Path("C:/duplicacy-e2e")
# Get-ScheduledTaskInfo's LastTaskResult for a task that has never run
TASK_NEVER_RUN = 267011


def _generate_key(key_file: Path) -> None:
    subprocess.run(
        [str(SSH_KEYGEN), "-q", "-t", "ed25519", "-N", "", "-f", str(key_file)],
        check=True,
    )


def _openssh_loads_key(key_file: Path) -> subprocess.CompletedProcess:
    """ssh-keygen -y applies the same "UNPROTECTED PRIVATE KEY FILE" ACL
    check that sftp does before using a key, so no sftp server is needed."""
    return subprocess.run(
        [str(SSH_KEYGEN), "-y", "-f", str(key_file)],
        capture_output=True,
        text=True,
        check=False,
    )


def _icacls(path: Path, *args: str) -> None:
    result = subprocess.run(
        ["icacls", str(path), *args], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"icacls {args}:\n{result.stdout}{result.stderr}"


def _powershell(command: str) -> str:
    # Drop PSModulePath inherited from pwsh (the Actions runner's default
    # shell), which makes Windows PowerShell load PowerShell 7's copies of
    # modules like Microsoft.PowerShell.Security and fail to run Get-Acl
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, f"{command}:\n{result.stderr}"
    return result.stdout.strip()


def test_secure_key_file_makes_a_readable_key_usable(tmp_path):
    key_file = tmp_path / "id_ed25519_client"
    _generate_key(key_file)
    _icacls(key_file, "/grant", "*S-1-5-32-545:R")  # Users
    assert _openssh_loads_key(key_file).returncode != 0, (
        "expected OpenSSH to reject a key readable by Users"
    )

    setup.secure_key_file(key_file)

    result = _openssh_loads_key(key_file)
    assert result.returncode == 0, result.stderr


def test_secure_key_file_reclaims_a_system_only_key(tmp_path):
    """A key owned by, and only accessible to, SYSTEM can't be read by an
    administrator running setup unless secure_key_file takes ownership
    first."""
    key_file = tmp_path / "id_ed25519_client"
    _generate_key(key_file)
    # Each step needs access the next one takes away: changing the owner
    # needs WRITE_OWNER and the last ACL change WRITE_DAC, both of which
    # this process only has through the Administrators and user entries
    _icacls(key_file, "/grant:r", "*S-1-5-18:F")
    _icacls(key_file, "/setowner", "*S-1-5-18")
    _icacls(
        key_file,
        "/inheritance:r",
        "/remove:g",
        "*S-1-5-32-544",
        getpass.getuser(),
    )
    assert _openssh_loads_key(key_file).returncode != 0, (
        "expected a SYSTEM-only key to be unreadable before secure_key_file"
    )

    setup.secure_key_file(key_file)

    result = _openssh_loads_key(key_file)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def clean_machine_state():
    yield
    subprocess.run(
        ["schtasks", "/Delete", "/TN", setup.WINDOWS_TASK_NAME, "/F"],
        capture_output=True,
        check=False,
    )


def _run_duplicacy(binary: Path, cwd: Path, *args: str, **env: str) -> None:
    result = subprocess.run(
        [str(binary), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        check=False,
    )
    assert result.returncode == 0, f"duplicacy {args}:\n{result.stdout}{result.stderr}"


def _wait_for_task_run(timeout: float = 600) -> int:
    """Polls until the scheduled task has run to completion, returning its
    exit code."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state, last_result = _powershell(
            f"$t = Get-ScheduledTask -TaskName '{setup.WINDOWS_TASK_NAME}'; "
            "$t.State; ($t | Get-ScheduledTaskInfo).LastTaskResult"
        ).splitlines()
        if state == "Ready" and int(last_result) != TASK_NEVER_RUN:
            return int(last_result)
        time.sleep(5)
    raise AssertionError(f"scheduled task didn't finish within {timeout}s")


def test_scheduled_task_backs_up_as_system(clean_machine_state):
    """Sets up C:\\duplicacy the way setup would (mklink /D symlink,
    encrypted local destination, config.yaml in its Windows default
    location with every other setting defaulted), registers the task with
    install_windows_scheduled_task, starts it, and checks that it backed
    up successfully as SYSTEM."""
    assert setup.running_as_root(), "must run from an elevated prompt"
    duplicacy_test_binary = os.environ["DUPLICACY_TEST_BINARY"]

    basedir = main.DEFAULT_DUPLICACY_BASEDIR
    backup_directory = basedir / "backup"
    target = E2E_DIR / "target"
    storage = E2E_DIR / "storage"
    for directory in (backup_directory, target, storage):
        directory.mkdir(parents=True)
    (target / "file1.txt").write_text("hello world\n")
    subprocess.run(
        ["cmd", "/c", "mklink", "/D", str(backup_directory / "data"), str(target)],
        check=True,
    )

    duplicacy_binary = main.duplicacy_binary_path(basedir)
    duplicacy_binary.parent.mkdir(parents=True)
    shutil.copy(duplicacy_test_binary, duplicacy_binary)
    password = "e2e-password"
    _run_duplicacy(
        duplicacy_binary,
        backup_directory,
        "init",
        "-e",
        "e2e-client",
        str(storage),
        DUPLICACY_PASSWORD=password,
    )
    _run_duplicacy(
        duplicacy_binary,
        backup_directory,
        "set",
        "-key",
        "password",
        "-value",
        password,
    )

    setup.write_config(
        main.default_config_path(),
        {
            "healthchecks_uuid": "00000000-0000-0000-0000-000000000000",
            "backup_directories": [str(backup_directory)],
        },
    )

    setup.install_windows_scheduled_task(main.default_config_path(), basedir)
    assert (
        _powershell(
            f"(Get-ScheduledTask -TaskName '{setup.WINDOWS_TASK_NAME}').Principal.UserId"
        )
        == "SYSTEM"
    )

    subprocess.run(["schtasks", "/Run", "/TN", setup.WINDOWS_TASK_NAME], check=True)
    exit_code = _wait_for_task_run()

    lastrun_log = basedir / "logs" / f"duplicacy.{main.short_hostname()}.lastrun.txt"
    log_text = lastrun_log.read_text(encoding="utf-8") if lastrun_log.is_file() else ""
    assert exit_code == 0, f"task exited {exit_code}; lastrun log:\n{log_text}"
    assert (storage / "snapshots" / "e2e-client" / "1").is_file(), log_text
    assert "Run complete" in log_text
    assert _powershell(f"(Get-Acl '{lastrun_log}').Owner") == "NT AUTHORITY\\SYSTEM"
