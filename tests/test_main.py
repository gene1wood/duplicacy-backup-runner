import http.server
import json
import subprocess
import sys
import threading

import pytest
import yaml

from duplicacy_backup_runner import main

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX permissions or commands"
)


def test_load_config_requires_healthchecks_uuid(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({"backup_directories": ["/tmp/backup"]}))
    with pytest.raises(SystemExit):
        main.load_config(config_path)


def test_load_config_defaults(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({"healthchecks_uuid": "abc123"}))
    config = main.load_config(config_path)

    assert config.healthchecks_uuid == "abc123"
    basedir = main.DEFAULT_DUPLICACY_BASEDIR
    assert config.client_individual_id == main.short_hostname()
    assert config.backup_directories == [basedir / "backup"]
    assert config.duplicacy_basedir == basedir
    assert config.log_basedir == basedir / "logs"
    assert config.rate_limit_rate == 32
    assert config.duplicacy_binary == main.duplicacy_binary_path(basedir)
    assert config.internet_check_attempts == 5
    assert config.internet_check_delay == 5
    assert config.internet_check_url == "http://www.google.com/"
    assert config.duplicacy_version == main.DEFAULT_DUPLICACY_VERSION
    assert config.duplicacy_download_url is None
    assert config.filters_url is None


def test_load_config_overrides(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.dump(
            {
                "healthchecks_uuid": "abc123",
                "client_individual_id": "custom-client",
                "backup_directories": ["/data/a", "/data/b"],
                "duplicacy_basedir": "/srv/duplicacy",
                "rate_limit_ip": "10.0.0.5",
                "rate_limit_rate": 64,
                "internet_check_attempts": 1,
                "internet_check_delay": 0,
                "internet_check_url": "http://127.0.0.1:8000/",
            }
        )
    )
    config = main.load_config(config_path)

    assert config.client_individual_id == "custom-client"
    assert config.backup_directories == [main.Path("/data/a"), main.Path("/data/b")]
    assert config.log_basedir == main.Path("/srv/duplicacy/logs")
    assert config.rate_limit_ip == "10.0.0.5"
    assert config.rate_limit_rate == 64
    assert config.internet_check_attempts == 1
    assert config.internet_check_delay == 0
    assert config.internet_check_url == "http://127.0.0.1:8000/"


@pytest.mark.parametrize(
    "storage,expected",
    [
        ("sftp://alice@example.com:2222//backup", ("alice", "example.com", 2222)),
        ("sftp://bob@example.com//backup", ("bob", "example.com", 22)),
    ],
)
def test_parse_sftp_target(storage, expected):
    assert main.parse_sftp_target(storage) == expected


def test_read_storage_entries(tmp_path):
    duplicacy_dir = tmp_path / ".duplicacy"
    duplicacy_dir.mkdir()
    preferences = [
        {
            "name": "default",
            "id": "morpheus",
            "storage": "sftp://alice@example.com//backup",
            "encrypted": True,
            "keys": {"ssh_key_file": "/keys/id_ed25519", "password": "secret"},
        },
        {"name": "local", "storage": "/local-backup"},
    ]
    (duplicacy_dir / "preferences").write_text(json.dumps(preferences))

    entries = main.read_storage_entries(tmp_path)

    assert entries == [
        main.StorageEntry(
            name="default",
            storage="sftp://alice@example.com//backup",
            encrypted=True,
            ssh_key_file=main.Path("/keys/id_ed25519"),
            password="secret",
            id="morpheus",
        ),
        main.StorageEntry(
            name="local",
            storage="/local-backup",
            encrypted=False,
            ssh_key_file=None,
            password=None,
        ),
    ]


@pytest.mark.parametrize(
    "storage,expected",
    [
        ("sftp://alice@example.com//backup", False),
        ("/local-duplicacy-backup/backup", True),
        ("relative/path", True),
    ],
)
def test_is_local_storage(storage, expected):
    assert main.is_local_storage(storage) is expected


def _entry(name, storage, **overrides):
    return main.StorageEntry.from_preferences(
        {"name": name, "storage": storage, **overrides}
    )


def test_find_local_entry_returns_the_local_one():
    remote = _entry("default", "sftp://alice@example.com//backup")
    local = _entry("local", "/local-backup")

    assert main.find_local_entry([remote, local]) == local


def test_find_local_entry_returns_none_when_all_remote():
    remote = _entry("default", "sftp://alice@example.com//backup")

    assert main.find_local_entry([remote]) is None


@pytest.mark.parametrize(
    "storage,expected",
    [
        ("sftp://alice@example.com/backup", "backup"),
        ("sftp://alice@example.com/archive/", "archive"),
        ("sftp://alice@example.com//backup", "backup"),
    ],
)
def test_derive_remote_name(storage, expected):
    assert main.derive_remote_name(storage) == expected


def test_has_non_symlink_directories_flags_real_subdirectory(tmp_path):
    (tmp_path / ".duplicacy").mkdir()
    (tmp_path / "real_subdir").mkdir()

    assert main.has_non_symlink_directories(tmp_path) is True


def test_has_non_symlink_directories_allows_symlinked_directory(tmp_path):
    target = tmp_path.parent / "target_dir"
    target.mkdir()
    (tmp_path / ".duplicacy").mkdir()
    (tmp_path / "linked").symlink_to(target)

    assert main.has_non_symlink_directories(tmp_path) is False


CONFIG_PATH = main.Path("/etc/duplicacy-backup-runner/config.yaml")


def _generated_known_hosts(known_hosts_string):
    return (
        "# Generated by duplicacy-backup-runner from known_hosts_string in\n"
        f"# {CONFIG_PATH}\n"
        "# Edit that setting instead: changes made here are overwritten on every run.\n"
        f"{known_hosts_string}"
    )


def test_ensure_known_hosts_writes_when_missing(tmp_path):
    (tmp_path / "keys").mkdir()
    run_logger = _CapturingRunLogger()
    path = main.ensure_known_hosts(
        tmp_path, "example.com ssh-ed25519 AAAA...\n", CONFIG_PATH, run_logger
    )

    assert path.read_text() == _generated_known_hosts(
        "example.com ssh-ed25519 AAAA...\n"
    )
    assert run_logger.warnings == []


def test_ensure_known_hosts_skips_write_when_unchanged(tmp_path):
    (tmp_path / "keys").mkdir()
    content = "example.com ssh-ed25519 AAAA...\n"
    known_hosts_path = tmp_path / "keys" / "known_hosts"
    known_hosts_path.write_text(_generated_known_hosts(content))
    original_mtime = known_hosts_path.stat().st_mtime_ns
    run_logger = _CapturingRunLogger()

    main.ensure_known_hosts(tmp_path, content, CONFIG_PATH, run_logger)

    assert known_hosts_path.stat().st_mtime_ns == original_mtime
    assert run_logger.warnings == []


def test_ensure_known_hosts_adds_header_without_warning_to_legacy_file(tmp_path):
    (tmp_path / "keys").mkdir()
    content = "example.com ssh-ed25519 AAAA...\n"
    known_hosts_path = tmp_path / "keys" / "known_hosts"
    known_hosts_path.write_text(content)
    run_logger = _CapturingRunLogger()

    main.ensure_known_hosts(tmp_path, content, CONFIG_PATH, run_logger)

    assert known_hosts_path.read_text() == _generated_known_hosts(content)
    assert run_logger.warnings == []


def test_ensure_known_hosts_warns_about_hand_edited_entries(tmp_path):
    (tmp_path / "keys").mkdir()
    known_hosts_path = tmp_path / "keys" / "known_hosts"
    known_hosts_path.write_text(
        _generated_known_hosts("one.example.com ssh-ed25519 AAAA\n")
        + "two.example.com ssh-ed25519 BBBB\n"
    )
    run_logger = _CapturingRunLogger()

    main.ensure_known_hosts(
        tmp_path, "one.example.com ssh-ed25519 AAAA\n", CONFIG_PATH, run_logger
    )

    assert known_hosts_path.read_text() == _generated_known_hosts(
        "one.example.com ssh-ed25519 AAAA\n"
    )
    assert len(run_logger.warnings) == 1
    assert "two.example.com ssh-ed25519 BBBB" in run_logger.warnings[0]
    assert "one.example.com" not in run_logger.warnings[0]
    assert str(CONFIG_PATH) in run_logger.warnings[0]


def test_ensure_known_hosts_leaves_file_alone_when_unset(tmp_path):
    (tmp_path / "keys").mkdir()
    known_hosts_path = tmp_path / "keys" / "known_hosts"
    known_hosts_path.write_text("hand managed\n")
    run_logger = _CapturingRunLogger()

    path = main.ensure_known_hosts(tmp_path, None, CONFIG_PATH, run_logger)

    assert path == known_hosts_path
    assert known_hosts_path.read_text() == "hand managed\n"
    assert run_logger.warnings == []


def test_parse_ls_ln_owner_finds_named_entry():
    ls_output = (
        "drwxr-xr-x    2 1000     1000         4096 Jan  1 00:00 backup\n"
        "-rw-r--r--    1 1000     1000            0 Jan  1 00:00 readme.txt\n"
    )
    assert main.parse_ls_ln_owner(ls_output) == "1000"


def test_parse_ls_ln_owner_missing_entry_returns_none():
    ls_output = "-rw-r--r--    1 1000     1000            0 Jan  1 00:00 readme.txt\n"
    assert main.parse_ls_ln_owner(ls_output) is None


def _fake_sftp_run(stdout="", returncode=0):
    return lambda commands, *args, **kwargs: subprocess.CompletedProcess(
        commands, returncode, stdout=stdout, stderr=""
    )


def test_storage_initialized_over_sftp_true_when_chunks_present(monkeypatch):
    monkeypatch.setattr(
        main,
        "sftp_run",
        _fake_sftp_run(
            "drwxr-xr-x    2 1000     1000         4096 Jan  1 00:00 chunks\n"
            "drwxr-xr-x    2 1000     1000         4096 Jan  1 00:00 snapshots\n"
        ),
    )

    assert (
        main.storage_initialized_over_sftp(
            "alice",
            "example.com",
            22,
            main.Path("/keys/id"),
            main.Path("/keys/known_hosts"),
            "backup",
        )
        is True
    )


def test_storage_initialized_over_sftp_true_when_listing_is_path_prefixed(monkeypatch):
    # `sftp ls -ln backup` prints entries as "backup/chunks", not "chunks"
    monkeypatch.setattr(
        main,
        "sftp_run",
        _fake_sftp_run(
            "drwxr-xr-x    2 1000     1000         4096 Jan  1 00:00 backup/chunks\n"
            "drwxr-xr-x    2 1000     1000         4096 Jan  1 00:00 backup/snapshots\n"
        ),
    )

    assert (
        main.storage_initialized_over_sftp(
            "alice",
            "example.com",
            22,
            main.Path("/keys/id"),
            main.Path("/keys/known_hosts"),
            "backup",
        )
        is True
    )


def test_storage_initialized_over_sftp_false_when_chunks_missing(monkeypatch):
    monkeypatch.setattr(
        main,
        "sftp_run",
        _fake_sftp_run(
            "drwxr-xr-x    2 1000     1000         4096 Jan  1 00:00 snapshots\n"
        ),
    )

    assert (
        main.storage_initialized_over_sftp(
            "alice",
            "example.com",
            22,
            main.Path("/keys/id"),
            main.Path("/keys/known_hosts"),
            "backup",
        )
        is False
    )


def test_storage_initialized_over_sftp_false_when_directory_missing(monkeypatch):
    monkeypatch.setattr(main, "sftp_run", _fake_sftp_run(returncode=1))

    assert (
        main.storage_initialized_over_sftp(
            "alice",
            "example.com",
            22,
            main.Path("/keys/id"),
            main.Path("/keys/known_hosts"),
            "backup",
        )
        is False
    )


def test_wait_for_internet_succeeds_on_first_try(monkeypatch):
    """A successful check shouldn't retry or sleep at all -- if it did,
    a real check succeeding would still cost time for no reason."""
    calls = []

    class _FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def fake_urlopen(url, timeout):
        calls.append(url)
        return _FakeResponse()

    def fail_if_called(*args, **kwargs):
        raise AssertionError("a successful check should never sleep")

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(main.time, "sleep", fail_if_called)

    result = main.wait_for_internet(
        _NullRunLogger(), attempts=5, delay=5, url="http://example.invalid/"
    )

    assert result is True
    assert calls == ["http://example.invalid/"]


def test_wait_for_internet_returns_false_after_exhausting_attempts(monkeypatch):
    def fake_urlopen(url, timeout):
        raise main.urllib.error.URLError("blocked")

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)

    result = main.wait_for_internet(
        _NullRunLogger(), attempts=3, delay=0, url="http://example.invalid/"
    )

    assert result is False


def test_tail_of_file_returns_last_n_lines(tmp_path):
    path = tmp_path / "run.log"
    path.write_text("\n".join(f"line {i}" for i in range(1, 21)) + "\n")

    assert main.tail_of_file(path, 3) == "line 18\nline 19\nline 20"


def test_tail_of_file_handles_fewer_lines_than_requested(tmp_path):
    path = tmp_path / "run.log"
    path.write_text("only line\n")

    assert main.tail_of_file(path, 13) == "only line"


def _make_config(**overrides):
    defaults = {
        "healthchecks_uuid": "uuid",
        "client_individual_id": "client",
        "backup_directories": [main.Path("/opt/duplicacy/backup")],
        "duplicacy_basedir": main.Path("/opt/duplicacy"),
        "log_basedir": main.Path("/opt/duplicacy/logs"),
        "lock_file": main.Path("/var/lock/duplicacy-backup"),
        "rate_limit_ip": None,
        "rate_limit_rate": 32,
        "known_hosts_string": None,
        "log_level": "INFO",
        "internet_check_attempts": 5,
        "internet_check_delay": 5,
        "internet_check_url": "http://www.google.com/",
        "duplicacy_version": main.DEFAULT_DUPLICACY_VERSION,
        "duplicacy_download_url": None,
        "filters_url": None,
    }
    defaults.update(overrides)
    return main.Config(**defaults)


class _NullRunLogger:
    def info(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass

    def debug(self, message):
        pass


def test_plan_destinations_single_entry_skips_rsa_check(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("check_rsa_encryption should not run for a single entry")

    monkeypatch.setattr(main, "check_rsa_encryption", fail_if_called)
    entry = _entry("default", "/local-backup", encrypted=False)

    plan = main.plan_destinations([entry], _make_config(), _NullRunLogger(), "run-id")

    assert plan == [main.PlannedDestination(entry)]


def test_plan_destinations_no_local_entry_backs_up_independently(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError(
            "check_rsa_encryption should not run without a local entry"
        )

    monkeypatch.setattr(main, "check_rsa_encryption", fail_if_called)
    default = _entry("default", "sftp://alice@example.com//backup")
    offsite = _entry("offsite", "sftp://bob@example.com//backup")

    plan = main.plan_destinations(
        [default, offsite], _make_config(), _NullRunLogger(), "run-id"
    )

    assert plan == [main.PlannedDestination(default), main.PlannedDestination(offsite)]


def test_plan_destinations_rsa_encrypted_backs_up_independently(monkeypatch):
    monkeypatch.setattr(
        main, "check_rsa_encryption", lambda *a, **k: main.EncryptionStatus.RSA
    )
    remote = _entry("default", "sftp://alice@example.com//backup")
    local = _entry("local", "/local-backup", encrypted=True)

    plan = main.plan_destinations(
        [remote, local], _make_config(), _NullRunLogger(), "run-id"
    )

    assert plan == [main.PlannedDestination(remote), main.PlannedDestination(local)]


def test_plan_destinations_not_rsa_copies_from_local(monkeypatch):
    monkeypatch.setattr(
        main, "check_rsa_encryption", lambda *a, **k: main.EncryptionStatus.NOT_RSA
    )
    remote = _entry("default", "sftp://alice@example.com//backup")
    local = _entry("local", "/local-backup", encrypted=False)

    plan = main.plan_destinations(
        [remote, local], _make_config(), _NullRunLogger(), "run-id"
    )

    assert plan == [
        main.PlannedDestination(local),
        main.PlannedDestination(remote, copy_source="local"),
    ]


def test_plan_destinations_hard_fails_when_rsa_status_unknown(monkeypatch):
    monkeypatch.setattr(
        main, "check_rsa_encryption", lambda *a, **k: main.EncryptionStatus.UNKNOWN
    )
    remote = _entry("default", "sftp://alice@example.com//backup")
    local = _entry("local", "/local-backup", encrypted=True)

    plan = main.plan_destinations(
        [remote, local], _make_config(), _NullRunLogger(), "run-id"
    )

    assert plan is None


def test_check_rsa_encryption_not_rsa_when_not_encrypted():
    entry = _entry("local", "/local-backup", encrypted=False)

    result = main.check_rsa_encryption(
        entry, _make_config(), _NullRunLogger(), "run-id"
    )

    assert result is main.EncryptionStatus.NOT_RSA


def test_check_rsa_encryption_unknown_when_password_missing(monkeypatch):
    monkeypatch.setattr(main, "ping_healthchecks", lambda *a, **k: True)
    entry = _entry("local", "/local-backup", encrypted=True)

    result = main.check_rsa_encryption(
        entry, _make_config(), _NullRunLogger(), "run-id"
    )

    assert result is main.EncryptionStatus.UNKNOWN


def test_check_rsa_encryption_detects_rsa_from_duplicacy_info_output(monkeypatch):
    monkeypatch.setattr(main, "ping_healthchecks", lambda *a, **k: True)

    def fake_run(cmd, **kwargs):
        assert kwargs["env"]["DUPLICACY_PASSWORD"] == "secret"
        return subprocess.CompletedProcess(
            cmd, 0, stdout="RSA public key: ...\n", stderr=""
        )

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    entry = _entry(
        "local", "/local-backup", encrypted=True, keys={"password": "secret"}
    )

    result = main.check_rsa_encryption(
        entry, _make_config(), _NullRunLogger(), "run-id"
    )

    assert result is main.EncryptionStatus.RSA


def test_check_rsa_encryption_not_rsa_when_no_rsa_line(monkeypatch):
    monkeypatch.setattr(main, "ping_healthchecks", lambda *a, **k: True)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout="Compression level: 100\n", stderr=""
        )

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    entry = _entry(
        "local", "/local-backup", encrypted=True, keys={"password": "secret"}
    )

    result = main.check_rsa_encryption(
        entry, _make_config(), _NullRunLogger(), "run-id"
    )

    assert result is main.EncryptionStatus.NOT_RSA


def test_check_rsa_encryption_unknown_when_probe_fails(monkeypatch):
    monkeypatch.setattr(main, "ping_healthchecks", lambda *a, **k: True)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="wrong password")

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    entry = _entry(
        "local", "/local-backup", encrypted=True, keys={"password": "secret"}
    )

    result = main.check_rsa_encryption(
        entry, _make_config(), _NullRunLogger(), "run-id"
    )

    assert result is main.EncryptionStatus.UNKNOWN


def test_resolve_rate_limit_returns_none_without_ip():
    assert main.resolve_rate_limit(None, 32) is None


@posix_only
def test_resolve_rate_limit_matches_exact_address(monkeypatch):
    def fake_run(cmd, **kwargs):
        payload = [{"addr_info": [{"local": "192.168.1.50"}]}]
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    assert main.resolve_rate_limit("192.168.1.50", 32) == 32


@posix_only
def test_resolve_rate_limit_does_not_match_address_prefix(monkeypatch):
    """Regression test: a naive substring match would incorrectly treat
    "192.168.1.5" as present on a host whose real address is
    "192.168.1.50"."""

    def fake_run(cmd, **kwargs):
        payload = [{"addr_info": [{"local": "192.168.1.50"}]}]
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    assert main.resolve_rate_limit("192.168.1.5", 32) is None


def test_resolve_rate_limit_windows_uses_resolved_host_addresses(monkeypatch):
    monkeypatch.setattr(main, "IS_WINDOWS", True)
    monkeypatch.setattr(
        main.socket,
        "getaddrinfo",
        lambda host, port: [(None, None, None, "", ("192.168.1.50", 0))],
    )

    assert main.resolve_rate_limit("192.168.1.50", 32) == 32
    assert main.resolve_rate_limit("192.168.1.5", 32) is None


def test_can_modify_destination_true_for_local():
    assert main.can_modify_destination(None) is True


def test_can_modify_destination_false_when_owned_by_root():
    sftp_target = main.SftpTarget(
        name="default",
        client="alice",
        server="example.com",
        port=22,
        key_file=main.Path("/keys/id_ed25519"),
        remote_root="backup",
        writable=False,
    )
    assert main.can_modify_destination(sftp_target) is False


def test_duplicacy_init_env_none_when_nothing_needed():
    assert main.duplicacy_init_env(None, None) is None


def test_duplicacy_init_env_passes_password_and_ssh_key_file(tmp_path):
    key = tmp_path / "id_ed25519"
    env = main.duplicacy_init_env("secret", key)
    assert env["DUPLICACY_PASSWORD"] == "secret"
    assert env["DUPLICACY_SSH_KEY_FILE"] == str(key)


@posix_only
def test_run_duplicacy_does_not_wait_on_stdin(tmp_path):
    """A prompt duplicacy wasn't given an answer for must fail fast rather
    than block forever reading the terminal."""
    returncode = main.run_duplicacy(
        ["sh", "-c", "read answer || exit 3"], tmp_path, _NullRunLogger()
    )
    assert returncode == 3


def test_find_rsa_public_keys_empty_when_dir_missing(tmp_path):
    assert main.find_rsa_public_keys(tmp_path / "keys") == []


def test_find_rsa_public_keys_detects_pem_public_key(tmp_path):
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()
    (keys_dir / "id_ed25519_host").write_text("not a key\n")
    (keys_dir / "id_ed25519_host.pub").write_text("ssh-ed25519 AAAA... comment\n")
    rsa_key = keys_dir / "rsa_public.pem"
    rsa_key.write_text(
        "-----BEGIN PUBLIC KEY-----\nMIIBIjANBg...\n-----END PUBLIC KEY-----\n"
    )

    assert main.find_rsa_public_keys(keys_dir) == [rsa_key]


def test_find_rsa_public_keys_detects_pkcs1_form(tmp_path):
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()
    rsa_key = keys_dir / "rsa_public.pem"
    rsa_key.write_text(
        "-----BEGIN RSA PUBLIC KEY-----\nMIIBCgKC...\n-----END RSA PUBLIC KEY-----\n"
    )

    assert main.find_rsa_public_keys(keys_dir) == [rsa_key]


def test_find_rsa_public_keys_returns_every_match(tmp_path):
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()
    first = keys_dir / "a.pem"
    second = keys_dir / "b.pem"
    for path in (first, second):
        path.write_text("-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n")

    assert main.find_rsa_public_keys(keys_dir) == [first, second]


def test_find_rsa_public_keys_ignores_unrelated_files(tmp_path):
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()
    (keys_dir / "known_hosts").write_text("example.com ssh-ed25519 AAAA...\n")
    (keys_dir / "subdir").mkdir()

    assert main.find_rsa_public_keys(keys_dir) == []


def test_initialize_storage_not_encrypted_runs_plain_init(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        main,
        "run_duplicacy",
        lambda args, cwd, run_logger, env=None: calls.append((args, env)) or 0,
    )
    entry = _entry("local", "/local-backup", encrypted=False)
    config = _make_config(duplicacy_basedir=tmp_path / "duplicacy")

    result = main.initialize_storage(
        entry, entry.storage, config, _NullRunLogger(), "run-id"
    )

    assert result is True
    args, env = calls[0]
    assert "-e" not in args
    assert "-key" not in args
    assert args[-2:] == [config.client_individual_id, "/local-backup"]
    assert env is None


def test_initialize_storage_encrypted_without_password_fails(monkeypatch):
    monkeypatch.setattr(main, "ping_healthchecks", lambda *a, **k: True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not attempt to run duplicacy without a password")

    monkeypatch.setattr(main, "run_duplicacy", fail_if_called)
    entry = _entry("local", "/local-backup", encrypted=True)

    result = main.initialize_storage(
        entry, entry.storage, _make_config(), _NullRunLogger(), "run-id"
    )

    assert result is False


def test_initialize_storage_encrypted_passes_password_and_dash_e(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        main,
        "run_duplicacy",
        lambda args, cwd, run_logger, env=None: calls.append((args, env)) or 0,
    )
    entry = _entry(
        "local", "/local-backup", encrypted=True, keys={"password": "secret"}
    )
    config = _make_config(duplicacy_basedir=tmp_path / "duplicacy")

    result = main.initialize_storage(
        entry, entry.storage, config, _NullRunLogger(), "run-id"
    )

    assert result is True
    args, env = calls[0]
    assert "-e" in args
    assert "-key" not in args
    assert env["DUPLICACY_PASSWORD"] == "secret"


def test_initialize_storage_finds_rsa_key_and_passes_dash_key(monkeypatch, tmp_path):
    keys_dir = tmp_path / "duplicacy" / "keys"
    keys_dir.mkdir(parents=True)
    rsa_key = keys_dir / "rsa_public.pem"
    rsa_key.write_text("-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n")
    calls = []
    monkeypatch.setattr(
        main,
        "run_duplicacy",
        lambda args, cwd, run_logger, env=None: calls.append((args, env)) or 0,
    )
    entry = _entry(
        "local", "/local-backup", encrypted=True, keys={"password": "secret"}
    )
    config = _make_config(duplicacy_basedir=tmp_path / "duplicacy")

    result = main.initialize_storage(
        entry, entry.storage, config, _NullRunLogger(), "run-id"
    )

    assert result is True
    args, _env = calls[0]
    assert args[args.index("-key") + 1] == str(rsa_key)


def test_initialize_storage_multiple_rsa_keys_refuses_to_guess(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "ping_healthchecks", lambda *a, **k: True)
    keys_dir = tmp_path / "duplicacy" / "keys"
    keys_dir.mkdir(parents=True)
    for name in ("a.pem", "b.pem"):
        (keys_dir / name).write_text(
            "-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n"
        )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not run duplicacy when the RSA key is ambiguous")

    monkeypatch.setattr(main, "run_duplicacy", fail_if_called)
    entry = _entry(
        "local", "/local-backup", encrypted=True, keys={"password": "secret"}
    )
    config = _make_config(duplicacy_basedir=tmp_path / "duplicacy")

    result = main.initialize_storage(
        entry, entry.storage, config, _NullRunLogger(), "run-id"
    )

    assert result is False


def test_initialize_storage_reports_failure_when_init_fails(monkeypatch):
    monkeypatch.setattr(main, "ping_healthchecks", lambda *a, **k: True)
    monkeypatch.setattr(main, "run_duplicacy", lambda *a, **k: 1)
    entry = _entry("local", "/local-backup", encrypted=False)

    result = main.initialize_storage(
        entry, entry.storage, _make_config(), _NullRunLogger(), "run-id"
    )

    assert result is False


def test_preflight_destination_local_initializes_when_chunks_missing(
    monkeypatch, tmp_path
):
    storage = tmp_path / "storage"
    storage.mkdir()
    calls = []
    monkeypatch.setattr(
        main, "initialize_storage", lambda *a, **k: calls.append(a) or True
    )
    entry = _entry("local", str(storage), encrypted=False)

    result = main.preflight_destination(
        entry,
        tmp_path / "repo",
        _make_config(),
        _NullRunLogger(),
        "run-id",
        tmp_path / "known_hosts",
    )

    assert result.ok is True
    assert len(calls) == 1


def test_preflight_destination_local_skips_init_when_chunks_present(
    monkeypatch, tmp_path
):
    storage = tmp_path / "storage"
    (storage / "chunks").mkdir(parents=True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not initialize an already-initialized destination")

    monkeypatch.setattr(main, "initialize_storage", fail_if_called)
    entry = _entry("local", str(storage), encrypted=False)

    result = main.preflight_destination(
        entry,
        tmp_path / "repo",
        _make_config(),
        _NullRunLogger(),
        "run-id",
        tmp_path / "known_hosts",
    )

    assert result.ok is True


def test_preflight_destination_local_fails_when_init_fails(monkeypatch, tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setattr(main, "initialize_storage", lambda *a, **k: False)
    entry = _entry("local", str(storage), encrypted=False)

    result = main.preflight_destination(
        entry,
        tmp_path / "repo",
        _make_config(),
        _NullRunLogger(),
        "run-id",
        tmp_path / "known_hosts",
    )

    assert result.ok is False


def test_preflight_destination_local_relative_storage_resolves_against_backup_directory(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "relative-storage" / "chunks").mkdir(parents=True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("chunks already exist relative to the backup directory")

    monkeypatch.setattr(main, "initialize_storage", fail_if_called)
    entry = _entry("local", "relative-storage", encrypted=False)

    result = main.preflight_destination(
        entry,
        repo,
        _make_config(),
        _NullRunLogger(),
        "run-id",
        tmp_path / "known_hosts",
    )

    assert result.ok is True


def test_preflight_destination_sftp_initializes_when_not_initialized(monkeypatch):
    monkeypatch.setattr(main, "test_sftp_connectivity", lambda *a, **k: True)
    monkeypatch.setattr(main, "storage_initialized_over_sftp", lambda *a, **k: False)
    calls = []
    monkeypatch.setattr(
        main, "initialize_storage", lambda *a, **k: calls.append(a) or True
    )
    monkeypatch.setattr(main, "get_remote_directory_owner", lambda *a, **k: "1000")
    entry = _entry(
        "default",
        "sftp://alice@example.com//backup",
        keys={"ssh_key_file": "/keys/id_ed25519"},
    )

    result = main.preflight_destination(
        entry,
        main.Path("/repo"),
        _make_config(),
        _NullRunLogger(),
        "run-id",
        main.Path("/known_hosts"),
    )

    assert result.ok is True
    assert len(calls) == 1


def test_preflight_destination_sftp_skips_init_when_already_initialized(monkeypatch):
    monkeypatch.setattr(main, "test_sftp_connectivity", lambda *a, **k: True)
    monkeypatch.setattr(main, "storage_initialized_over_sftp", lambda *a, **k: True)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not initialize an already-initialized destination")

    monkeypatch.setattr(main, "initialize_storage", fail_if_called)
    monkeypatch.setattr(main, "get_remote_directory_owner", lambda *a, **k: "1000")
    entry = _entry(
        "default",
        "sftp://alice@example.com//backup",
        keys={"ssh_key_file": "/keys/id_ed25519"},
    )

    result = main.preflight_destination(
        entry,
        main.Path("/repo"),
        _make_config(),
        _NullRunLogger(),
        "run-id",
        main.Path("/known_hosts"),
    )

    assert result.ok is True


def test_held_lock_blocks_concurrent_run(tmp_path):
    lock_file = tmp_path / "lock"
    with main.held_lock(lock_file) as first_acquired:
        assert first_acquired is True
        with main.held_lock(lock_file) as second_acquired:
            assert second_acquired is False


def test_held_lock_releases_on_exit(tmp_path):
    lock_file = tmp_path / "lock"
    with main.held_lock(lock_file) as acquired:
        assert acquired is True

    with main.held_lock(lock_file) as acquired_again:
        assert acquired_again is True


class _CapturingRunLogger(_NullRunLogger):
    def __init__(self):
        self.errors = []
        self.messages = []
        self.warnings = []

    def info(self, message):
        self.messages.append(message)

    def warning(self, message):
        self.warnings.append(message)

    def close_log_file(self, path):
        pass

    def error(self, message):
        self.errors.append(message)


@posix_only
def test_provision_basedir_creates_directories_and_chmods_keys(tmp_path):
    duplicacy_basedir = tmp_path / "duplicacy"
    log_basedir = tmp_path / "logs"

    main.provision_basedir(duplicacy_basedir, log_basedir, _NullRunLogger())

    assert (duplicacy_basedir / "bin").is_dir()
    assert (duplicacy_basedir / "keys").is_dir()
    assert log_basedir.is_dir()
    assert main.stat.S_IMODE((duplicacy_basedir / "keys").stat().st_mode) == 0o700


@posix_only
def test_provision_basedir_is_idempotent_when_already_correct(tmp_path):
    duplicacy_basedir = tmp_path / "duplicacy"
    log_basedir = tmp_path / "logs"
    main.provision_basedir(duplicacy_basedir, log_basedir, _NullRunLogger())

    main.provision_basedir(duplicacy_basedir, log_basedir, _NullRunLogger())

    assert main.stat.S_IMODE((duplicacy_basedir / "keys").stat().st_mode) == 0o700


def test_provision_basedir_logs_error_on_mkdir_failure(monkeypatch, tmp_path):
    def fail_mkdir(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(main.Path, "mkdir", fail_mkdir)
    run_logger = _CapturingRunLogger()

    main.provision_basedir(tmp_path / "duplicacy", tmp_path / "logs", run_logger)

    assert run_logger.errors


def test_provision_duplicacy_binary_skips_download_when_already_present(
    monkeypatch, tmp_path
):
    duplicacy_binary = tmp_path / "duplicacy"
    duplicacy_binary.write_text("existing binary")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("download_file should not run when binary already exists")

    monkeypatch.setattr(main, "download_file", fail_if_called)

    main.provision_duplicacy_binary(duplicacy_binary, "3.2.5", None, _NullRunLogger())

    assert duplicacy_binary.read_text() == "existing binary"


class _FixedPayloadHandler(http.server.BaseHTTPRequestHandler):
    payload = b"fake binary contents"

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, format, *args):
        pass


@pytest.fixture
def fixed_payload_http_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _FixedPayloadHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@posix_only
def test_provision_duplicacy_binary_downloads_and_chmods_when_missing(
    tmp_path, fixed_payload_http_server
):
    duplicacy_binary = tmp_path / "duplicacy"

    main.provision_duplicacy_binary(
        duplicacy_binary, "3.2.5", fixed_payload_http_server, _NullRunLogger()
    )

    assert duplicacy_binary.read_bytes() == _FixedPayloadHandler.payload
    assert main.stat.S_IMODE(duplicacy_binary.stat().st_mode) == 0o755


def test_provision_duplicacy_binary_logs_error_on_download_failure(tmp_path):
    run_logger = _CapturingRunLogger()

    main.provision_duplicacy_binary(
        tmp_path / "duplicacy", "3.2.5", "http://127.0.0.1:1/nope", run_logger
    )

    assert run_logger.errors


@pytest.mark.parametrize(
    "version,expected",
    [
        (
            "3.2.5",
            (
                "https://github.com/gilbertchen/duplicacy/releases/download/"
                "v3.2.5/duplicacy_linux_x64_3.2.5"
            ),
        ),
    ],
)
def test_default_duplicacy_download_url_format(monkeypatch, version, expected):
    monkeypatch.setattr(main, "IS_WINDOWS", False)
    assert main.default_duplicacy_download_url(version) == expected


def test_default_duplicacy_download_url_on_windows(monkeypatch):
    monkeypatch.setattr(main, "IS_WINDOWS", True)

    assert main.default_duplicacy_download_url("3.2.5").endswith(
        "v3.2.5/duplicacy_win_x64_3.2.5.exe"
    )


def test_duplicacy_binary_path_adds_exe_on_windows(monkeypatch):
    monkeypatch.setattr(main, "IS_WINDOWS", True)

    assert main.duplicacy_binary_path(main.Path("base")).name == "duplicacy.exe"


def test_short_hostname_lowercased_only_on_windows(monkeypatch):
    monkeypatch.setattr(main.socket, "gethostname", lambda: "DESKTOP-ABC.example")
    monkeypatch.setattr(main, "IS_WINDOWS", True)
    assert main.short_hostname() == "desktop-abc"

    monkeypatch.setattr(main, "IS_WINDOWS", False)
    assert main.short_hostname() == "DESKTOP-ABC"


def test_default_config_path_is_in_basedir_on_windows(monkeypatch):
    monkeypatch.setattr(main, "IS_WINDOWS", True)

    assert main.default_config_path() == main.DEFAULT_DUPLICACY_BASEDIR / "config.yaml"


def test_chmod_if_needed_is_noop_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "IS_WINDOWS", True)
    path = tmp_path / "key"
    path.write_text("key")
    run_logger = _CapturingRunLogger()

    main.chmod_if_needed(path, 0o600, run_logger, dry_run=True)

    assert run_logger.messages == []


def test_sftp_executable_prefers_openssh_feature_path_on_windows(monkeypatch, tmp_path):
    sftp = tmp_path / "sftp.exe"
    sftp.write_text("")
    monkeypatch.setattr(main, "IS_WINDOWS", True)
    monkeypatch.setattr(main, "WINDOWS_SFTP_PATH", sftp)
    monkeypatch.setattr(main.shutil, "which", lambda name: "C:/Git/usr/bin/sftp")

    assert main.sftp_executable() == str(sftp)


def test_sftp_executable_uses_path_when_openssh_feature_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "IS_WINDOWS", True)
    monkeypatch.setattr(main, "WINDOWS_SFTP_PATH", tmp_path / "missing.exe")
    monkeypatch.setattr(main.shutil, "which", lambda name: "C:/Git/usr/bin/sftp")

    assert main.sftp_executable() == "C:/Git/usr/bin/sftp"


@posix_only
def test_provision_ssh_key_permissions_chmods_referenced_key(tmp_path):
    duplicacy_dir = tmp_path / ".duplicacy"
    duplicacy_dir.mkdir()
    key_file = tmp_path.parent / "id_ed25519"
    key_file.write_text("private key")
    key_file.chmod(0o644)
    preferences = [
        {
            "name": "default",
            "storage": "sftp://alice@example.com//backup",
            "encrypted": False,
            "keys": {"ssh_key_file": str(key_file)},
        }
    ]
    (duplicacy_dir / "preferences").write_text(json.dumps(preferences))

    main.provision_ssh_key_permissions(tmp_path, _NullRunLogger())

    assert main.stat.S_IMODE(key_file.stat().st_mode) == 0o600


def test_provision_ssh_key_permissions_noop_when_preferences_missing(tmp_path):
    main.provision_ssh_key_permissions(tmp_path, _NullRunLogger())


def test_provision_ssh_key_permissions_logs_error_on_malformed_preferences(tmp_path):
    duplicacy_dir = tmp_path / ".duplicacy"
    duplicacy_dir.mkdir()
    (duplicacy_dir / "preferences").write_text("not json")
    run_logger = _CapturingRunLogger()

    main.provision_ssh_key_permissions(tmp_path, run_logger)

    assert run_logger.errors


def test_provision_filters_downloads_when_missing_and_url_set(
    tmp_path, fixed_payload_http_server
):
    (tmp_path / ".duplicacy").mkdir()

    main.provision_filters(tmp_path, fixed_payload_http_server, _NullRunLogger())

    assert (tmp_path / ".duplicacy" / "filters").read_bytes() == (
        _FixedPayloadHandler.payload
    )


def test_provision_filters_noop_without_filters_url(tmp_path):
    (tmp_path / ".duplicacy").mkdir()

    main.provision_filters(tmp_path, None, _NullRunLogger())

    assert not (tmp_path / ".duplicacy" / "filters").exists()


def test_provision_filters_noop_when_file_already_present(tmp_path, monkeypatch):
    duplicacy_dir = tmp_path / ".duplicacy"
    duplicacy_dir.mkdir()
    (duplicacy_dir / "filters").write_text("existing filters")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("download_file should not run when filters already exist")

    monkeypatch.setattr(main, "download_file", fail_if_called)

    main.provision_filters(tmp_path, "http://example.invalid/filters", _NullRunLogger())

    assert (duplicacy_dir / "filters").read_text() == "existing filters"


def test_provision_skips_backup_directories_that_dont_exist_yet(monkeypatch, tmp_path):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("per-backup_directory provisioning should not run")

    monkeypatch.setattr(main, "provision_basedir", lambda *a, **k: None)
    monkeypatch.setattr(main, "provision_duplicacy_binary", lambda *a, **k: None)
    monkeypatch.setattr(main, "provision_ssh_key_permissions", fail_if_called)
    monkeypatch.setattr(main, "provision_filters", fail_if_called)

    config = _make_config(backup_directories=[tmp_path / "does-not-exist"])
    main.provision(config, _NullRunLogger())


def test_is_empty_backup_directory_true_for_empty(tmp_path):
    assert main.is_empty_backup_directory(tmp_path) is True


def test_is_empty_backup_directory_false_when_entries_present(tmp_path):
    (tmp_path / ".duplicacy").mkdir()
    assert main.is_empty_backup_directory(tmp_path) is False


def test_run_backup_directory_fails_clearly_on_empty_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "ping_healthchecks", lambda *a, **k: True)

    outcome = main.run_backup_directory(
        tmp_path,
        _make_config(),
        _NullRunLogger(),
        "run-id",
        main.Path("/known_hosts"),
        None,
    )

    assert outcome.ok is False


def test_notify_healthchecks_pings_normally(monkeypatch):
    calls = []
    monkeypatch.setattr(
        main, "ping_healthchecks", lambda *a, **k: calls.append((a, k)) or True
    )

    main.notify_healthchecks(_make_config(), "run-id", "/start", data="hi")

    assert calls == [(("uuid", "run-id", "/start"), {"data": "hi"})]


def test_notify_healthchecks_skipped_in_dry_run(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("a dry run must not ping healthchecks.io")

    monkeypatch.setattr(main, "ping_healthchecks", fail_if_called)

    main.notify_healthchecks(_make_config(dry_run=True), "run-id", "/start")
    main.log_error(_NullRunLogger(), _make_config(dry_run=True), "run-id", "boom")


def test_initialize_storage_dry_run_validates_but_doesnt_run_init(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("a dry run must not run duplicacy init")

    monkeypatch.setattr(main, "run_duplicacy", fail_if_called)
    run_logger = _CapturingRunLogger()
    entry = _entry("local", "/local-backup", encrypted=False)

    result = main.initialize_storage(
        entry, entry.storage, _make_config(dry_run=True), run_logger, "run-id"
    )

    assert result is True
    assert any("would initialize storage" in m for m in run_logger.messages)


def test_initialize_storage_dry_run_still_fails_without_password(monkeypatch):
    monkeypatch.setattr(main, "run_duplicacy", lambda *a, **k: 0)
    entry = _entry("local", "/local-backup", encrypted=True)

    result = main.initialize_storage(
        entry, entry.storage, _make_config(dry_run=True), _NullRunLogger(), "run-id"
    )

    assert result is False


def test_preflight_destination_local_dry_run_marks_uninitialized(monkeypatch, tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setattr(main, "run_duplicacy", lambda *a, **k: 0)
    entry = _entry("local", str(storage), encrypted=False)

    result = main.preflight_destination(
        entry,
        tmp_path / "repo",
        _make_config(dry_run=True),
        _NullRunLogger(),
        "run-id",
        tmp_path / "known_hosts",
    )

    assert result.ok is True
    assert result.initialized is False


def test_preflight_destination_sftp_dry_run_skips_owner_lookup_when_uninitialized(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(main, "test_sftp_connectivity", lambda *a, **k: True)
    monkeypatch.setattr(main, "storage_initialized_over_sftp", lambda *a, **k: False)
    monkeypatch.setattr(main, "run_duplicacy", lambda *a, **k: 0)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not look up the owner of an uninitialized dir")

    monkeypatch.setattr(main, "get_remote_directory_owner", fail_if_called)
    entry = _entry(
        "default",
        "sftp://alice@example.com//backup",
        keys={"ssh_key_file": "/keys/id_ed25519"},
    )

    result = main.preflight_destination(
        entry,
        tmp_path,
        _make_config(dry_run=True),
        _NullRunLogger(),
        "run-id",
        tmp_path / "known_hosts",
    )

    assert result.ok is True
    assert result.initialized is False
    assert result.sftp_target is not None


def test_process_destination_dry_run_skips_uninitialized_destination(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        main,
        "preflight_destination",
        lambda *a, **k: main.PreflightResult(ok=True, initialized=False),
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("nothing to back up to or prune yet")

    monkeypatch.setattr(main, "run_duplicacy", fail_if_called)

    outcome = main.process_destination(
        _entry("local", "/local-backup"),
        None,
        tmp_path,
        _make_config(dry_run=True),
        _NullRunLogger(),
        "run-id",
        tmp_path / "known_hosts",
        None,
    )

    assert outcome.ok is True


@pytest.mark.parametrize("dry_run", [False, True])
def test_backup_and_prune_pass_dry_run_flag_only_in_dry_run(
    monkeypatch, tmp_path, dry_run
):
    calls = []
    monkeypatch.setattr(
        main,
        "run_duplicacy",
        lambda args, cwd, run_logger, env=None: calls.append(args) or 0,
    )
    config = _make_config(dry_run=dry_run)
    entry = _entry("local", "/local-backup")

    main.run_backup_to_destination(
        entry, tmp_path, config, _NullRunLogger(), "run-id", None
    )
    main.duplicacy_prune_destination(
        entry, tmp_path, config, _NullRunLogger(), "run-id"
    )

    assert [("-dry-run" in args) for args in calls] == [dry_run, dry_run]


def test_run_copy_to_destination_dry_run_doesnt_run_copy(monkeypatch, tmp_path):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("duplicacy copy has no -dry-run, so must not run")

    monkeypatch.setattr(main, "run_duplicacy", fail_if_called)
    run_logger = _CapturingRunLogger()

    result = main.run_copy_to_destination(
        _entry("offsite", "sftp://alice@example.com//backup"),
        "local",
        tmp_path,
        _make_config(dry_run=True),
        run_logger,
        "run-id",
        None,
    )

    assert result is True
    assert any("Dry run: would copy" in m for m in run_logger.messages)


@posix_only
def test_provision_dry_run_changes_nothing(tmp_path):
    backup_directory = tmp_path / "backup"
    (backup_directory / ".duplicacy").mkdir(parents=True)
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("key")
    key_file.chmod(0o644)
    (backup_directory / ".duplicacy" / "preferences").write_text(
        json.dumps(
            [
                {
                    "name": "default",
                    "storage": "sftp://alice@example.com//backup",
                    "keys": {"ssh_key_file": str(key_file)},
                }
            ]
        )
    )
    config = _make_config(
        duplicacy_basedir=tmp_path / "duplicacy",
        log_basedir=tmp_path / "logs",
        backup_directories=[backup_directory],
        duplicacy_download_url="http://example.invalid/duplicacy",
        filters_url="http://example.invalid/filters",
        dry_run=True,
    )
    run_logger = _CapturingRunLogger()

    main.provision(config, run_logger)

    assert not (tmp_path / "duplicacy").exists()
    assert not (tmp_path / "logs").exists()
    assert not (backup_directory / ".duplicacy" / "filters").exists()
    assert main.stat.S_IMODE(key_file.stat().st_mode) == 0o644
    joined = "\n".join(run_logger.messages)
    assert "would create" in joined
    assert "would download duplicacy binary" in joined
    assert "would chmod" in joined
    assert "would download filters" in joined


def test_ensure_known_hosts_write_dir_leaves_real_file_untouched(tmp_path):
    (tmp_path / "keys").mkdir()
    real = tmp_path / "keys" / "known_hosts"
    real.write_text("old content\n")
    write_dir = tmp_path / "scratch"
    write_dir.mkdir()
    run_logger = _CapturingRunLogger()

    path = main.ensure_known_hosts(
        tmp_path, "new content\n", CONFIG_PATH, run_logger, write_dir
    )

    assert path == write_dir / "known_hosts"
    assert path.read_text() == _generated_known_hosts("new content\n")
    assert real.read_text() == "old content\n"
    # A dry run still reports the drift a real run would overwrite
    assert "old content" in run_logger.warnings[0]


def test_ensure_known_hosts_write_dir_used_even_when_real_file_matches(tmp_path):
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "known_hosts").write_text(
        _generated_known_hosts("new content\n")
    )
    write_dir = tmp_path / "scratch"
    write_dir.mkdir()

    path = main.ensure_known_hosts(
        tmp_path, "new content\n", CONFIG_PATH, _CapturingRunLogger(), write_dir
    )

    assert path.read_text() == _generated_known_hosts("new content\n")


def test_run_logger_without_log_paths_is_console_only(tmp_path, capsys):
    run_logger = main.RunLogger(None, None, None, main.logging.INFO)

    run_logger.info("status line")
    run_logger.write_raw("raw duplicacy output")

    out = capsys.readouterr().out
    assert "status line" in out
    assert "raw duplicacy output" in out


def test_main_rejects_dry_run_with_setup():
    with pytest.raises(SystemExit):
        main.main(["--dry-run", "setup"])


def test_sftp_run_reports_timeout_instead_of_hanging(monkeypatch, tmp_path):
    # A child that outlives sftp while holding its output handles open, like
    # ssh.exe does under Windows' sftp.exe
    fake_sftp = tmp_path / "fake_sftp.py"
    fake_sftp.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "time.sleep(30)\n"
    )
    monkeypatch.setattr(main, "sftp_executable", lambda: sys.executable)
    real_popen = subprocess.Popen
    monkeypatch.setattr(
        main.subprocess,
        "Popen",
        lambda cmd, **kwargs: real_popen([sys.executable, str(fake_sftp)], **kwargs),
    )

    result = main.sftp_run(
        "pwd\n", "alice", "example.com", 22, tmp_path / "key", tmp_path / "kh", 1
    )

    assert result.returncode == -1
    assert "timed out" in result.stderr


def test_upload_log_deletes_run_log_that_is_still_being_logged_to(
    monkeypatch, tmp_path
):
    run_log, persistent_log, lastrun_log = main.create_log_files(
        tmp_path, "client", "run.txt"
    )
    run_logger = main.RunLogger(run_log, persistent_log, lastrun_log, 0)
    run_logger.info("before upload")
    monkeypatch.setattr(main, "sftp_run", _fake_sftp_run())
    sftp_target = main.SftpTarget(
        name="default",
        client="alice",
        server="example.com",
        port=22,
        key_file=main.Path("/keys/id_ed25519"),
        remote_root="backup",
        writable=True,
    )

    main.upload_log(
        run_log, "run.txt", sftp_target, tmp_path / "kh", "host", run_logger
    )
    run_logger.info("after upload")

    assert not run_log.exists()
    assert "after upload" in persistent_log.read_text()


def _upload_target(writable):
    return main.SftpTarget(
        name="default",
        client="alice",
        server="example.com",
        port=22,
        key_file=main.Path("/keys/id_ed25519"),
        remote_root="backup",
        writable=writable,
    )


def test_sftp_run_reads_commands_as_a_batch_file(monkeypatch, tmp_path):
    # Without -b, sftp keeps going past a failed command and exits 0
    captured = {}
    real_popen = subprocess.Popen

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return real_popen([sys.executable, "-c", ""], **kwargs)

    monkeypatch.setattr(main.subprocess, "Popen", fake_popen)

    main.sftp_run(
        "pwd\n", "alice", "example.com", 22, tmp_path / "key", tmp_path / "kh"
    )

    assert captured["cmd"][1:3] == ["-b", "-"]


def test_upload_log_creates_logs_directory_and_ignores_symlink_failures(
    monkeypatch, tmp_path
):
    commands = []

    def fake_sftp_run(command_text, *args, **kwargs):
        commands.append(command_text)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(main, "sftp_run", fake_sftp_run)
    run_log = tmp_path / "run.txt"
    run_log.write_text("log\n")

    uploaded = main.upload_log(
        run_log,
        "run.txt",
        _upload_target(writable=True),
        tmp_path / "kh",
        "host",
        _CapturingRunLogger(),
    )

    assert uploaded is True
    lines = commands[0].splitlines()
    assert lines[0] == '-mkdir "backup/logs"'
    assert lines[1] == f'put "{run_log}" "backup/logs/"'
    assert lines[2].startswith("-rm ")
    assert lines[3].startswith("-symlink ")


def test_upload_log_reports_failure_and_keeps_run_log(monkeypatch, tmp_path):
    monkeypatch.setattr(
        main,
        "sftp_run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, "", "dest open: Failure"),
    )
    run_log = tmp_path / "run.txt"
    run_log.write_text("log\n")
    run_logger = _CapturingRunLogger()

    uploaded = main.upload_log(
        run_log,
        "run.txt",
        _upload_target(writable=False),
        tmp_path / "kh",
        "host",
        run_logger,
    )

    assert uploaded is False
    assert run_log.exists()
    assert "dest open: Failure" in run_logger.errors[0]
    assert not any("log uploaded" in message for message in run_logger.messages)
