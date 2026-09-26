import io
import subprocess

import pytest
import yaml

from duplicacy_backup_runner import main, setup


def test_check_setup_prerequisites_reports_missing_binaries(monkeypatch):
    monkeypatch.setattr(
        setup.shutil,
        "which",
        lambda name: None if name == "openssl" else "/usr/bin/" + name,
    )

    assert setup.check_setup_prerequisites() == ["openssl"]


def test_check_setup_prerequisites_empty_when_all_present(monkeypatch):
    monkeypatch.setattr(setup.shutil, "which", lambda name: "/usr/bin/" + name)

    assert setup.check_setup_prerequisites() == []


def test_openssl_is_version_3_detects_version_string(monkeypatch):
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, stdout="OpenSSL 3.0.2 15 Mar 2022\n", stderr=""
        ),
    )

    assert setup.openssl_is_version_3() is True


def test_openssl_is_version_3_false_for_openssl_1(monkeypatch):
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, stdout="OpenSSL 1.1.1f  31 Mar 2020\n", stderr=""
        ),
    )

    assert setup.openssl_is_version_3() is False


def test_systemd_service_unit_content_includes_exec_command():
    content = setup.systemd_service_unit_content("/usr/bin/duplicacy-backup-runner")

    assert "ExecStart=/usr/bin/duplicacy-backup-runner" in content
    assert "Type=oneshot" in content


def test_systemd_timer_unit_content_has_randomized_delay():
    content = setup.systemd_timer_unit_content()

    assert "RandomizedDelaySec=10800" in content
    assert "WantedBy=timers.target" in content


def test_cron_d_content_includes_exec_command():
    content = setup.cron_d_content("/usr/bin/duplicacy-backup-runner")

    assert "/usr/bin/duplicacy-backup-runner" in content
    assert content.startswith("SHELL=/bin/bash\n")


def test_detect_init_system_returns_systemd_when_pid1_is_systemd(monkeypatch):
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, stdout="systemd\n", stderr=""
        ),
    )

    assert setup.detect_init_system() == "systemd"


def test_detect_init_system_returns_cron_otherwise(monkeypatch):
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, stdout="init\n", stderr=""
        ),
    )

    assert setup.detect_init_system() == "cron"


def test_resolve_console_script_path_prefers_which(monkeypatch):
    monkeypatch.setattr(
        setup.shutil, "which", lambda name: "/usr/local/bin/duplicacy-backup-runner"
    )

    assert (
        setup.resolve_console_script_path() == "/usr/local/bin/duplicacy-backup-runner"
    )


def test_resolve_console_script_path_falls_back_to_module_invocation(monkeypatch):
    monkeypatch.setattr(setup.shutil, "which", lambda name: None)

    result = setup.resolve_console_script_path()

    assert result.endswith("-m duplicacy_backup_runner.main")


def test_build_exec_command_omits_config_flag_for_default_path():
    command = setup.build_exec_command(
        "/usr/bin/duplicacy-backup-runner", main.default_config_path()
    )

    assert command == "/usr/bin/duplicacy-backup-runner"


def test_build_exec_command_includes_config_flag_for_explicit_path(tmp_path):
    config_path = tmp_path / "config.yaml"

    command = setup.build_exec_command("/usr/bin/duplicacy-backup-runner", config_path)

    assert command == f"/usr/bin/duplicacy-backup-runner --config {config_path}"


def test_install_systemd_units_writes_service_and_timer(monkeypatch, tmp_path):
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="", stderr=""),
    )

    setup.install_systemd_units("/usr/bin/duplicacy-backup-runner", unit_dir=tmp_path)

    assert (tmp_path / "duplicacy-backup-runner.service").exists()
    assert (tmp_path / "duplicacy-backup-runner.timer").exists()


def test_install_systemd_units_does_not_overwrite_existing_files(monkeypatch, tmp_path):
    (tmp_path / "duplicacy-backup-runner.service").write_text("custom content")
    (tmp_path / "duplicacy-backup-runner.timer").write_text("custom content")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("systemctl should not run when the timer already exists")

    monkeypatch.setattr(setup.subprocess, "run", fail_if_called)

    setup.install_systemd_units("/usr/bin/duplicacy-backup-runner", unit_dir=tmp_path)

    assert (
        tmp_path / "duplicacy-backup-runner.service"
    ).read_text() == "custom content"


def test_install_cron_d_entry_writes_file(tmp_path):
    cron_path = tmp_path / "cron"

    setup.install_cron_d_entry("/usr/bin/duplicacy-backup-runner", cron_path=cron_path)

    assert "/usr/bin/duplicacy-backup-runner" in cron_path.read_text()


def test_install_cron_d_entry_does_not_overwrite_existing_file(tmp_path):
    cron_path = tmp_path / "cron"
    cron_path.write_text("custom content")

    setup.install_cron_d_entry("/usr/bin/duplicacy-backup-runner", cron_path=cron_path)

    assert cron_path.read_text() == "custom content"


def test_install_scheduled_run_prints_instead_of_writing_when_not_root(
    monkeypatch, capsys
):
    monkeypatch.setattr(setup, "running_as_root", lambda: False)
    monkeypatch.setattr(setup, "detect_init_system", lambda: "cron")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("nothing should be written when not root")

    monkeypatch.setattr(setup, "install_systemd_units", fail_if_called)
    monkeypatch.setattr(setup, "install_cron_d_entry", fail_if_called)

    setup.install_scheduled_run("/usr/bin/duplicacy-backup-runner")

    assert "/usr/bin/duplicacy-backup-runner" in capsys.readouterr().out


def test_load_existing_config_dict_returns_empty_when_missing(tmp_path):
    assert setup.load_existing_config_dict(tmp_path / "missing.yaml") == {}


def test_load_existing_config_dict_returns_parsed_yaml(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({"healthchecks_uuid": "abc"}))

    assert setup.load_existing_config_dict(config_path) == {"healthchecks_uuid": "abc"}


def test_merge_backup_directory_into_config_appends_new_entry():
    raw_config = {"backup_directories": ["/opt/duplicacy/backup"]}

    result = setup.merge_backup_directory_into_config(
        raw_config, main.Path("/data/repo")
    )

    assert result["backup_directories"] == ["/opt/duplicacy/backup", "/data/repo"]


def test_merge_backup_directory_into_config_is_idempotent_when_already_present():
    raw_config = {"backup_directories": ["/data/repo"]}

    result = setup.merge_backup_directory_into_config(
        raw_config, main.Path("/data/repo")
    )

    assert result["backup_directories"] == ["/data/repo"]


def test_write_config_creates_parent_dirs(tmp_path):
    config_path = tmp_path / "nested" / "config.yaml"

    setup.write_config(config_path, {"healthchecks_uuid": "abc"})

    assert yaml.safe_load(config_path.read_text()) == {"healthchecks_uuid": "abc"}


def test_write_config_writes_multiline_strings_as_literal_blocks(tmp_path):
    config_path = tmp_path / "config.yaml"
    known_hosts = "host ssh-rsa AAAA\nhost ssh-ed25519 BBBB\n"

    setup.write_config(
        config_path, {"healthchecks_uuid": "abc", "known_hosts_string": known_hosts}
    )

    content = config_path.read_text()
    assert "known_hosts_string: |\n  host ssh-rsa AAAA\n" in content
    assert "healthchecks_uuid: abc\n" in content
    assert yaml.safe_load(content)["known_hosts_string"] == known_hosts


def test_ask_multiline_reads_until_blank_line(monkeypatch):
    monkeypatch.setattr(
        setup.sys,
        "stdin",
        io.StringIO("host ssh-rsa AAAA\nhost ssh-ed25519 BBBB\n\nnext answer\n"),
    )

    result = setup.ask_multiline("known_hosts")

    assert result == "host ssh-rsa AAAA\nhost ssh-ed25519 BBBB\n"
    assert setup.sys.stdin.readline() == "next answer\n"


def test_ask_multiline_reprompts_on_empty_input(monkeypatch):
    monkeypatch.setattr(setup.sys, "stdin", io.StringIO("\nhost ssh-rsa AAAA\n"))

    assert setup.ask_multiline("known_hosts") == "host ssh-rsa AAAA\n"


def test_ask_multiline_aborts_on_eof_without_input(monkeypatch):
    monkeypatch.setattr(setup.sys, "stdin", io.StringIO(""))

    with pytest.raises(setup.click.Abort):
        setup.ask_multiline("known_hosts")


def test_prompt_for_healthchecks_uuid_retries_until_valid(monkeypatch):
    answers = iter(["not-a-uuid", "00000000-0000-0000-0000-000000000000"])
    monkeypatch.setattr(setup, "ask", lambda *a, **k: next(answers))

    result = setup.prompt_for_healthchecks_uuid()

    assert result == "00000000-0000-0000-0000-000000000000"


def test_duplicacy_set_builds_expected_args(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(setup.subprocess, "run", fake_run)

    setup.duplicacy_set(
        main.Path("/opt/duplicacy/bin/duplicacy"), tmp_path, "ssh_key_file", "/keys/id"
    )

    args, kwargs = calls[0]
    assert args == [
        "/opt/duplicacy/bin/duplicacy",
        "set",
        "-key",
        "ssh_key_file",
        "-value",
        "/keys/id",
    ]
    assert kwargs["cwd"] == tmp_path


def test_duplicacy_set_scopes_to_storage_when_given(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(setup.subprocess, "run", fake_run)

    setup.duplicacy_set(
        main.Path("/opt/duplicacy/bin/duplicacy"),
        tmp_path,
        "password",
        "secret",
        storage_name="default",
    )

    assert calls[0][-2:] == ["-storage", "default"]


def test_duplicacy_set_raises_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, stdout="", stderr="boom"),
    )

    with pytest.raises(SystemExit):
        setup.duplicacy_set(
            main.Path("/opt/duplicacy/bin/duplicacy"), tmp_path, "password", "secret"
        )


def test_running_as_root_true_for_uid_zero(monkeypatch):
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)
    assert setup.running_as_root() is True


def test_running_as_root_false_for_nonzero_uid(monkeypatch):
    monkeypatch.setattr(setup.os, "geteuid", lambda: 1000)
    assert setup.running_as_root() is False


def test_prompt_encryption_choice_declines_encryption(monkeypatch):
    monkeypatch.setattr(setup, "confirm", lambda *a, **k: False)

    encrypted, rsa_public_key = setup.prompt_encryption_choice(
        "client", main.Path("/keys")
    )

    assert encrypted is False
    assert rsa_public_key is None


def test_prompt_encryption_choice_sync_returns_no_key(monkeypatch):
    monkeypatch.setattr(setup, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(setup, "ask", lambda *a, **k: "sync")

    encrypted, rsa_public_key = setup.prompt_encryption_choice(
        "client", main.Path("/keys")
    )

    assert encrypted is True
    assert rsa_public_key is None


def test_prompt_encryption_choice_refuses_multiple_rsa_candidates(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(setup, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(setup, "ask", lambda *a, **k: "async")
    monkeypatch.setattr(
        main,
        "find_rsa_public_keys",
        lambda keys_dir: [tmp_path / "a.pem", tmp_path / "b.pem"],
    )

    with pytest.raises(SystemExit):
        setup.prompt_encryption_choice("client", tmp_path)


def test_prompt_encryption_choice_reuses_existing_rsa_key(monkeypatch, tmp_path):
    existing = tmp_path / "existing_public.pem"
    monkeypatch.setattr(setup, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(setup, "ask", lambda *a, **k: "async")
    monkeypatch.setattr(main, "find_rsa_public_keys", lambda keys_dir: [existing])

    def fail_if_called(*a, **k):
        raise AssertionError("should not generate a new keypair when one exists")

    monkeypatch.setattr(setup, "generate_rsa_keypair", fail_if_called)

    encrypted, rsa_public_key = setup.prompt_encryption_choice("client", tmp_path)

    assert encrypted is True
    assert rsa_public_key == existing
