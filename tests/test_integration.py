"""Integration tests that run a real `duplicacy` binary end-to-end.

These exist because check_rsa_encryption's expected output ("RSA public
key:") and every CLI flag this tool passes to duplicacy (-storage, -from/-to,
-limit-rate, ...) were derived by reading Duplicacy's Go source, not by
observing the real tool -- unit tests mock all of that away. These tests
were written and verified against duplicacy 3.2.5 (linux/amd64).

Skipped automatically unless a duplicacy binary can be found, via (in order):
  1. the DUPLICACY_TEST_BINARY environment variable
  2. tests/bin/duplicacy (gitignored -- drop a binary there for local runs;
     get one from https://github.com/gilbertchen/duplicacy/releases)
  3. `duplicacy` on PATH
"""

from __future__ import annotations

import http.server
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from duplicacy_backup_runner import main, setup


def _find_duplicacy_binary() -> Path | None:
    env_path = os.environ.get("DUPLICACY_TEST_BINARY")
    if env_path and Path(env_path).is_file():
        return Path(env_path)

    local_binary = Path(__file__).parent / "bin" / "duplicacy"
    if local_binary.is_file():
        return local_binary

    which = shutil.which("duplicacy")
    return Path(which) if which else None


DUPLICACY_BINARY = _find_duplicacy_binary()

pytestmark = pytest.mark.skipif(
    DUPLICACY_BINARY is None,
    reason=(
        "No duplicacy binary found. Set DUPLICACY_TEST_BINARY, place one at "
        "tests/bin/duplicacy, or put it on PATH -- see this file's module "
        "docstring."
    ),
)


class _NullRunLogger:
    def info(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass

    def debug(self, message: str) -> None:
        pass

    def write_raw(self, line: str) -> None:
        pass


class _CapturingRunLogger(_NullRunLogger):
    """Records info() messages so a test can assert on which code path
    actually ran (e.g. "copy" vs. an independent "backup")."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, message: str) -> None:
        self.messages.append(message)


def _install_duplicacy(duplicacy_basedir: Path) -> None:
    """Copies the real duplicacy binary to where Config.duplicacy_binary
    expects it (bin/duplicacy, or bin/duplicacy.exe on Windows)."""
    binary = main.duplicacy_binary_path(duplicacy_basedir)
    binary.parent.mkdir(parents=True)
    shutil.copy(DUPLICACY_BINARY, binary)
    binary.chmod(0o755)


def _make_config(tmp_path: Path, **overrides) -> main.Config:
    """A Config pointing at a private copy of the real duplicacy binary
    under tmp_path. internet_check_* is left at its normal default here --
    these tests call run_backup_directory/check_rsa_encryption directly,
    which never touch wait_for_internet, so it's inert; see
    test_main_backs_up_through_the_real_cli_entrypoint below for a test that
    does exercise it, against a local server rather than the real internet."""
    duplicacy_basedir = tmp_path / "duplicacy"
    _install_duplicacy(duplicacy_basedir)
    (duplicacy_basedir / "keys").mkdir()

    defaults = {
        "healthchecks_uuid": "test-uuid",
        "client_individual_id": "test-client",
        "backup_directories": [],
        "duplicacy_basedir": duplicacy_basedir,
        "log_basedir": duplicacy_basedir / "logs",
        "lock_file": tmp_path / "lock",
        "rate_limit_ip": None,
        "rate_limit_rate": 32,
        "known_hosts_string": None,
        "log_level": "ERROR",
        "internet_check_attempts": 5,
        "internet_check_delay": 5,
        "internet_check_url": "http://www.google.com/",
        "duplicacy_version": main.DEFAULT_DUPLICACY_VERSION,
        "duplicacy_download_url": None,
        "filters_url": None,
    }
    defaults.update(overrides)
    return main.Config(**defaults)


class _OKHandler(http.server.BaseHTTPRequestHandler):
    """Answers every request with a bare 200, standing in for "the internet
    is up" and for healthchecks.io without touching the real internet."""

    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.do_GET()

    def log_message(self, format: str, *args) -> None:
        pass  # silence BaseHTTPRequestHandler's default access logging


@pytest.fixture
def local_http_server(monkeypatch) -> Iterator[str]:
    """Starts a local HTTP server that always answers 200, for tests that
    need wait_for_internet's connectivity check to genuinely succeed without
    depending on (or simulating the absence of) real internet access. Also
    points healthchecks.io pings at it, so they neither reach the real
    service (as they would from CI) nor depend on it failing fast."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _OKHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        main, "HEALTHCHECKS_BASE_URL", f"http://127.0.0.1:{server.server_port}"
    )
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _run_duplicacy(cwd: Path, *args: str, env: dict[str, str] | None = None) -> None:
    """Runs the real duplicacy binary, failing the test loudly (with its
    output) if the command didn't succeed -- this is test setup, not
    something we're asserting behavior about."""
    result = subprocess.run(
        [str(DUPLICACY_BINARY), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        check=False,
    )
    assert result.returncode == 0, (
        f"duplicacy {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
    )


def _set_preferences_password(repo: Path, storage_name: str, password: str) -> None:
    """Embeds password into preferences.json's keys.password for
    storage_name, the same way a real deployment does -- duplicacy itself
    only saves passwords to the OS keyring by default, never to
    preferences.json, and this sandbox has no keyring backend."""
    _run_duplicacy(
        repo, "set", "-key", "password", "-value", password, "-storage", storage_name
    )


def test_single_local_destination_backup_and_prune(tmp_path):
    repo = tmp_path / "repo"
    storage = tmp_path / "storage"
    repo.mkdir()
    storage.mkdir()
    (repo / "file1.txt").write_text("hello world\n")

    _run_duplicacy(repo, "init", "default", str(storage))

    config = _make_config(tmp_path, backup_directories=[repo])
    outcome = main.run_backup_directory(
        repo, config, _NullRunLogger(), "run-id", tmp_path / "known_hosts", None
    )

    assert outcome.ok is True
    assert (storage / "snapshots" / "default" / "1").is_file()


def test_backs_up_to_local_destination_with_no_chunks_directory_yet(tmp_path):
    """Simulates a destination that's listed in preferences but whose
    storage was never actually initialized (or was wiped after the fact):
    the repository's .duplicacy/preferences is real, but the storage
    directory it points at is emptied right after, so run_backup_directory
    has to notice the missing "chunks" directory itself and run `duplicacy
    init` against it before backing up."""
    repo = tmp_path / "repo"
    storage = tmp_path / "storage"
    repo.mkdir()
    storage.mkdir()
    (repo / "file1.txt").write_text("hello world\n")

    _run_duplicacy(repo, "init", "default", str(storage))
    shutil.rmtree(storage)
    storage.mkdir()
    assert not (storage / "chunks").exists()

    config = _make_config(tmp_path, backup_directories=[repo])
    outcome = main.run_backup_directory(
        repo, config, _NullRunLogger(), "run-id", tmp_path / "known_hosts", None
    )

    assert outcome.ok is True
    assert (storage / "chunks").is_dir()
    assert (storage / "snapshots" / "default" / "1").is_file()


@pytest.mark.skipif(
    shutil.which("openssl") is None,
    reason="requires openssl to generate an RSA keypair",
)
def test_initializes_storage_with_rsa_when_public_key_present_in_keys_dir(tmp_path):
    """A destination's preferences carry encrypted=true and a password, but
    -- since it's never been initialized -- there's no config file yet to
    ask Duplicacy whether it's RSA-encrypted (that's what
    check_rsa_encryption does for an already-initialized destination).
    initialize_storage instead has to intuit RSA usage from whether an RSA
    public key is sitting in duplicacy_basedir/keys (find_rsa_public_keys).
    This proves the resulting storage really ends up RSA-encrypted, not
    just that -key was passed on the command line."""
    repo = tmp_path / "repo"
    storage = tmp_path / "storage"
    repo.mkdir()
    storage.mkdir()
    (repo / "file1.txt").write_text("hello world\n")

    private_key = tmp_path / "rsa_private.pem"
    public_key = tmp_path / "rsa_public.pem"
    subprocess.run(
        ["openssl", "genrsa", "-out", str(private_key), "2048"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["openssl", "rsa", "-in", str(private_key), "-pubout", "-out", str(public_key)],
        check=True,
        capture_output=True,
    )

    password = "testpass123"
    _run_duplicacy(
        repo,
        "init",
        "-e",
        "default",
        str(storage),
        env={"DUPLICACY_PASSWORD": password},
    )
    _set_preferences_password(repo, "default", password)
    shutil.rmtree(storage)
    storage.mkdir()

    config = _make_config(tmp_path, backup_directories=[repo])
    (config.duplicacy_basedir / "keys" / "rsa_public.pem").write_text(
        public_key.read_text()
    )

    outcome = main.run_backup_directory(
        repo, config, _NullRunLogger(), "run-id", tmp_path / "known_hosts", None
    )
    assert outcome.ok is True

    entry = main.read_storage_entries(repo)[0]
    result = main.check_rsa_encryption(entry, config, _NullRunLogger(), "run-id")
    assert result is main.EncryptionStatus.RSA


def test_two_local_destinations_uses_copy_not_independent_backup(tmp_path):
    repo = tmp_path / "repo"
    storage_a = tmp_path / "storageA"
    storage_b = tmp_path / "storageB"
    repo.mkdir()
    storage_a.mkdir()
    storage_b.mkdir()
    (repo / "file1.txt").write_text("hello world\n")

    password = "testpass123"
    # A copy-compatible second storage needs the *source* storage's password
    # too (to read its existing config), hence both env vars here.
    _run_duplicacy(
        repo,
        "init",
        "-e",
        "default",
        str(storage_a),
        env={"DUPLICACY_PASSWORD": password},
    )
    _run_duplicacy(
        repo,
        "add",
        "-copy",
        "default",
        "-e",
        "local2",
        "default",
        str(storage_b),
        env={"DUPLICACY_PASSWORD": password, "DUPLICACY_LOCAL2_PASSWORD": password},
    )
    _set_preferences_password(repo, "default", password)
    _set_preferences_password(repo, "local2", password)

    config = _make_config(tmp_path, backup_directories=[repo])
    run_logger = _CapturingRunLogger()
    outcome = main.run_backup_directory(
        repo, config, run_logger, "run-id", tmp_path / "known_hosts", None
    )

    assert outcome.ok is True
    assert (storage_a / "snapshots" / "default" / "1").is_file()
    assert (storage_b / "snapshots" / "default" / "1").is_file()

    # The real assertion: local2 was populated via `duplicacy copy`, not an
    # independent `duplicacy backup -storage local2` -- both would leave a
    # snapshot behind, so only the log distinguishes which path ran.
    assert any(
        "Beginning copy of" in m and "'local2'" in m for m in run_logger.messages
    )
    assert not any(
        "Beginning backup of" in m and "'local2'" in m for m in run_logger.messages
    )


def test_check_rsa_encryption_against_real_non_rsa_storage(tmp_path):
    repo = tmp_path / "repo"
    storage = tmp_path / "storage"
    repo.mkdir()
    storage.mkdir()

    password = "testpass123"
    _run_duplicacy(
        repo,
        "init",
        "-e",
        "default",
        str(storage),
        env={"DUPLICACY_PASSWORD": password},
    )
    _set_preferences_password(repo, "default", password)

    entry = main.read_storage_entries(repo)[0]
    config = _make_config(tmp_path)

    result = main.check_rsa_encryption(entry, config, _NullRunLogger(), "run-id")

    assert result is main.EncryptionStatus.NOT_RSA


@pytest.mark.skipif(
    shutil.which("openssl") is None,
    reason="requires openssl to generate an RSA keypair",
)
def test_check_rsa_encryption_against_real_rsa_storage(tmp_path):
    repo = tmp_path / "repo"
    storage = tmp_path / "storage"
    repo.mkdir()
    storage.mkdir()

    private_key = tmp_path / "rsa_private.pem"
    public_key = tmp_path / "rsa_public.pem"
    subprocess.run(
        ["openssl", "genrsa", "-out", str(private_key), "2048"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["openssl", "rsa", "-in", str(private_key), "-pubout", "-out", str(public_key)],
        check=True,
        capture_output=True,
    )

    password = "testpass123"
    _run_duplicacy(
        repo,
        "init",
        "-e",
        "-key",
        str(public_key),
        "default",
        str(storage),
        env={"DUPLICACY_PASSWORD": password},
    )
    _set_preferences_password(repo, "default", password)

    entry = main.read_storage_entries(repo)[0]
    config = _make_config(tmp_path)

    result = main.check_rsa_encryption(entry, config, _NullRunLogger(), "run-id")

    assert result is main.EncryptionStatus.RSA


def test_main_backs_up_through_the_real_cli_entrypoint(tmp_path, local_http_server):
    """Runs main() itself (config file, locking, logging, healthchecks
    pings, and all) rather than calling a lower-level function directly.

    internet_check_url points at local_http_server, a real server that
    really answers 200 -- proving the connectivity check succeeds rather
    than just being disabled. A successful check needs no retries, so it
    costs nothing, and healthchecks.io pings go to the same local server.
    The elapsed-time assertion below is what actually distinguishes
    "the check succeeded quickly" from "the check silently failed and this
    only looks fast because something else broke first".
    """
    repo = tmp_path / "repo"
    storage = tmp_path / "storage"
    repo.mkdir()
    storage.mkdir()
    (repo / "file1.txt").write_text("hello world\n")
    _run_duplicacy(repo, "init", "default", str(storage))

    duplicacy_basedir = tmp_path / "duplicacy"
    _install_duplicacy(duplicacy_basedir)
    (duplicacy_basedir / "keys").mkdir()

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.dump(
            {
                "healthchecks_uuid": "00000000-0000-0000-0000-000000000000",
                "backup_directories": [str(repo)],
                "duplicacy_basedir": str(duplicacy_basedir),
                "lock_file": str(tmp_path / "lock"),
                "internet_check_url": local_http_server,
            }
        )
    )

    start = time.monotonic()
    exit_code = main.main(["--config", str(config_path)])
    elapsed = time.monotonic() - start

    assert exit_code == 0
    assert (storage / "snapshots" / "default" / "1").is_file()
    assert elapsed < 5


@pytest.mark.skipif(
    shutil.which("openssl") is None,
    reason="requires openssl to generate an RSA keypair",
)
def test_generate_rsa_keypair_produces_a_usable_public_key(tmp_path, monkeypatch):
    """openssl genrsa -aes256 (and the later -pubout decrypt) prompt for a
    passphrase interactively -- fine for the real setup TUI, which inherits
    the terminal, but this test passes it with -passout/-passin instead.
    Piping it to stdin works on Linux with no TTY, but OpenSSL on Windows
    prompts on the console and would wait there forever."""
    real_run = subprocess.run

    def fake_run(args, **kwargs):
        # Inserted right after the subcommand, since genrsa takes no
        # options after its trailing key size
        if "genrsa" in args:
            return real_run(
                [*args[:2], "-passout", "pass:testpassphrase", *args[2:]], **kwargs
            )
        if "-pubout" in args:
            return real_run(
                [*args[:2], "-passin", "pass:testpassphrase", *args[2:]], **kwargs
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr(setup.subprocess, "run", fake_run)

    public_key = setup.generate_rsa_keypair(tmp_path, "testclient")

    assert public_key.is_file()
    assert (tmp_path / "testclient_duplicacy_encryption_key_private.pem").is_file()
    assert main.find_rsa_public_keys(tmp_path) == [public_key]


def test_initialize_repository_creates_local_storage_and_preferences(tmp_path):
    repo = tmp_path / "repo"
    storage = tmp_path / "storage"
    repo.mkdir()
    storage.mkdir()

    ok = setup.initialize_repository(
        DUPLICACY_BINARY,
        repo,
        "testclient",
        str(storage),
        False,
        None,
        None,
        None,
        _NullRunLogger(),
    )

    assert ok is True
    assert (storage / "chunks").is_dir()
    assert (repo / ".duplicacy" / "preferences").is_file()


def test_initialize_repository_encrypted_with_password(tmp_path):
    repo = tmp_path / "repo"
    storage = tmp_path / "storage"
    repo.mkdir()
    storage.mkdir()

    ok = setup.initialize_repository(
        DUPLICACY_BINARY,
        repo,
        "testclient",
        str(storage),
        True,
        None,
        "testpass123",
        None,
        _NullRunLogger(),
    )

    assert ok is True
    entry = main.read_storage_entries(repo)[0]
    assert entry.encrypted is True


def test_setup_then_run_backs_up_successfully(tmp_path, monkeypatch, local_http_server):
    """Drives duplicacy-backup-runner setup with scripted answers (local,
    unencrypted destination), then runs a normal scheduled backup against
    the config it wrote -- proving the full setup -> scheduled-run handoff
    actually works, since every prompt boundary elsewhere is mocked."""
    repo = tmp_path / "repo"
    storage = tmp_path / "storage"
    target = tmp_path / "target"
    repo.mkdir()
    storage.mkdir()
    target.mkdir()
    (target / "file1.txt").write_text("hello world\n")
    (repo / "data").symlink_to(target)

    duplicacy_basedir = tmp_path / "duplicacy"
    _install_duplicacy(duplicacy_basedir)

    config_path = tmp_path / "config.yaml"

    asks = iter([str(storage), "", "00000000-0000-0000-0000-000000000000"])
    confirms = iter([False, False])  # not sftp; not encrypted
    monkeypatch.setattr(setup, "ask", lambda *a, **k: next(asks))
    monkeypatch.setattr(setup, "confirm", lambda *a, **k: next(confirms))
    monkeypatch.setattr(setup, "running_as_root", lambda: False)

    setup_exit_code = main.main(
        [
            "--config",
            str(config_path),
            "setup",
            "--duplicacy-basedir",
            str(duplicacy_basedir),
            "--backup-directory",
            str(repo),
        ]
    )

    assert setup_exit_code == 0
    assert (storage / "chunks").is_dir()
    assert (repo / ".duplicacy" / "preferences").is_file()
    assert config_path.is_file()

    raw_config = yaml.safe_load(config_path.read_text())
    raw_config["internet_check_url"] = local_http_server
    raw_config["lock_file"] = str(tmp_path / "lock")
    config_path.write_text(yaml.dump(raw_config))

    run_exit_code = main.main(["--config", str(config_path)])

    assert run_exit_code == 0
    assert any((storage / "snapshots").iterdir())


def test_setup_provisions_multiple_backup_directories(tmp_path, monkeypatch):
    """Runs setup with two --backup-directory options: each gets its own
    destination prompt and duplicacy init, and both end up in config.yaml's
    backup_directories."""
    repos = [tmp_path / "repo1", tmp_path / "repo2"]
    storages = [tmp_path / "storage1", tmp_path / "storage2"]
    target = tmp_path / "target"
    target.mkdir()
    for repo, storage in zip(repos, storages):
        repo.mkdir()
        storage.mkdir()
        (repo / "data").symlink_to(target)

    duplicacy_basedir = tmp_path / "duplicacy"
    _install_duplicacy(duplicacy_basedir)
    config_path = tmp_path / "config.yaml"

    asks = iter(
        [
            str(storages[0]),
            "",
            str(storages[1]),
            "",
            "00000000-0000-0000-0000-000000000000",
        ]
    )
    confirms = iter([False, False, False, False])  # not sftp; not encrypted (x2)
    monkeypatch.setattr(setup, "ask", lambda *a, **k: next(asks))
    monkeypatch.setattr(setup, "confirm", lambda *a, **k: next(confirms))
    monkeypatch.setattr(setup, "running_as_root", lambda: False)

    exit_code = main.main(
        [
            "--config",
            str(config_path),
            "setup",
            "--duplicacy-basedir",
            str(duplicacy_basedir),
            "--backup-directory",
            str(repos[0]),
            "--backup-directory",
            str(repos[1]),
        ]
    )

    assert exit_code == 0
    assert next(asks, None) is None
    for repo, storage in zip(repos, storages):
        assert (repo / ".duplicacy" / "preferences").is_file()
        assert (storage / "chunks").is_dir()
    raw_config = yaml.safe_load(config_path.read_text())
    assert raw_config["backup_directories"] == [str(repo) for repo in repos]


def test_setup_reuses_existing_preferences_without_prompting(tmp_path, monkeypatch):
    """Runs setup against a repository that already has
    .duplicacy/preferences (encrypted, password already set): the storage
    URL, encryption and password must come from preferences, so the only
    prompts left are the filters URL and healthchecks.io UUID, and
    duplicacy init isn't re-run."""
    repo = tmp_path / "repo"
    storage = tmp_path / "storage"
    target = tmp_path / "target"
    repo.mkdir()
    storage.mkdir()
    target.mkdir()
    (repo / "data").symlink_to(target)
    _run_duplicacy(
        repo,
        "init",
        "-e",
        "testclient",
        str(storage),
        env={"DUPLICACY_PASSWORD": "secret-password"},
    )
    _set_preferences_password(repo, "default", "secret-password")
    preferences_before = (repo / ".duplicacy" / "preferences").read_text()

    duplicacy_basedir = tmp_path / "duplicacy"
    _install_duplicacy(duplicacy_basedir)
    config_path = tmp_path / "config.yaml"

    def fail_confirm(*args, **kwargs):
        raise AssertionError(f"unexpected confirm prompt: {args}")

    def fail_initialize_repository(*args, **kwargs):
        raise AssertionError("duplicacy init must not be re-run")

    asks = iter(["", "00000000-0000-0000-0000-000000000000"])
    monkeypatch.setattr(setup, "ask", lambda *a, **k: next(asks))
    monkeypatch.setattr(setup, "confirm", fail_confirm)
    monkeypatch.setattr(setup, "initialize_repository", fail_initialize_repository)
    monkeypatch.setattr(setup, "running_as_root", lambda: False)

    exit_code = main.main(
        [
            "--config",
            str(config_path),
            "setup",
            "--duplicacy-basedir",
            str(duplicacy_basedir),
            "--backup-directory",
            str(repo),
        ]
    )

    assert exit_code == 0
    assert next(asks, None) is None
    assert (storage / "logs").is_dir()
    assert (repo / ".duplicacy" / "preferences").read_text() == preferences_before
    raw_config = yaml.safe_load(config_path.read_text())
    assert raw_config["backup_directories"] == [str(repo)]


def test_main_dry_run_changes_nothing(tmp_path, local_http_server, monkeypatch):
    """Runs main() --dry-run against a real repository whose storage has one
    snapshot already and a new file since: the real `backup -dry-run` and
    `prune -dry-run` must run and succeed without adding a snapshot, and
    nothing else a real run writes (log files, lock file, healthchecks.io
    pings) may happen."""

    def fail_if_called(*args, **kwargs):
        raise AssertionError("a dry run must not ping healthchecks.io")

    monkeypatch.setattr(main, "ping_healthchecks", fail_if_called)

    repo = tmp_path / "repo"
    storage = tmp_path / "storage"
    repo.mkdir()
    storage.mkdir()
    (repo / "file1.txt").write_text("hello world\n")
    _run_duplicacy(repo, "init", "default", str(storage))
    _run_duplicacy(repo, "backup")
    (repo / "file2.txt").write_text("new since the last backup\n")

    config = _make_config(tmp_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.dump(
            {
                "healthchecks_uuid": "00000000-0000-0000-0000-000000000000",
                "backup_directories": [str(repo)],
                "duplicacy_basedir": str(config.duplicacy_basedir),
                "lock_file": str(tmp_path / "lock"),
                "internet_check_url": local_http_server,
            }
        )
    )

    exit_code = main.main(["--config", str(config_path), "--dry-run"])

    assert exit_code == 0
    assert (storage / "snapshots" / "default" / "1").is_file()
    assert not (storage / "snapshots" / "default" / "2").exists()
    assert not config.log_basedir.exists()
    assert not (tmp_path / "lock").exists()


def test_dry_run_leaves_uninitialized_destination_uninitialized(tmp_path):
    repo = tmp_path / "repo"
    storage = tmp_path / "storage"
    repo.mkdir()
    storage.mkdir()
    (repo / "file1.txt").write_text("hello world\n")
    _run_duplicacy(repo, "init", "default", str(storage))
    shutil.rmtree(storage)
    storage.mkdir()

    config = _make_config(tmp_path, backup_directories=[repo], dry_run=True)
    run_logger = _CapturingRunLogger()
    outcome = main.run_backup_directory(
        repo, config, run_logger, "run-id", tmp_path / "known_hosts", None
    )

    assert outcome.ok is True
    assert list(storage.iterdir()) == []
    assert any("would initialize storage" in m for m in run_logger.messages)
