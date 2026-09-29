# duplicacy-backup-runner

Runs a scheduled [Duplicacy](https://duplicacy.com/) backup and reports progress to
[healthchecks.io](https://healthchecks.io/).

For each configured backup directory, it:

- Verifies every non-`.duplicacy` entry is a symlink (a real subdirectory would hide
  its contents from Duplicacy).
- Reads every destination from `.duplicacy/preferences` (a repository can back up to
  more than one storage). For sftp destinations, it checks connectivity and remote
  directory ownership to decide whether this host may prune it; each sftp entry needs
  its own `keys.ssh_key_file`, and its server's host key in `known_hosts_string`.
- Runs `duplicacy init` against any destination missing its "chunks" directory,
  using RSA encryption if exactly one RSA public key is found under
  `<duplicacy_basedir>/keys` (detected by content, not filename), otherwise plain
  password encryption. More than one candidate key is treated as a failure.
- When there's a local destination (a bare filesystem path) plus others, backs up to
  the local one first and replicates via the cheaper `duplicacy copy`, unless the
  local destination is RSA-encrypted (this tool never holds the private key needed to
  decrypt it for `copy`) — in which case it runs a full `duplicacy backup` against
  every destination instead. RSA status is always probed via `duplicacy info -e`; 
  if that can't be determined, the backup directory is skipped for this run.
- Prunes each destination after its backup/copy succeeds, where pruning is permitted.
- Logs to a per-run file, a persistent per-client log, and a "lastrun" log, and
  reports start/failure/success/log-tail events to healthchecks.io.
- Uploads the per-run log to the `logs/` directory of every destination, local
  or sftp, and (where pruning is permitted) points a
  `duplicacy.<host>.latest.txt` symlink at it.

A failure on one destination or backup directory doesn't abort the run — processing
continues with the rest, and the final log-upload/ping always happens.

## Installation

```bash
pip install duplicacy-backup-runner
```

## First-time host setup

On a new client host, after `pip install duplicacy-backup-runner`, run:

```bash
duplicacy-backup-runner setup
```

This provisions `<duplicacy_basedir>`, downloads the `duplicacy` binary, walks
you through SSH keys and sync/RSA encryption, runs `duplicacy init`, fetches
`.duplicacy/filters`, writes/updates `config.yaml`, and installs a systemd
timer or `/etc/cron.d` entry. It asks for backup directories one at a time until
you enter a blank line (or pass `--backup-directory` once per directory), and
each is set up in turn. A backup directory that doesn't exist is only created if
you confirm it, so a typo isn't provisioned as a new, empty directory. Run it
again to add another backup directory or destination to an existing setup. If a
backup directory already has a `.duplicacy/preferences`, its default
destination's storage URL, encryption, SSH key and password are reused rather
than prompted for, and `duplicacy init` is skipped. You're only asked for an sftp
server's `known_hosts` entries if `<duplicacy_basedir>/keys/known_hosts` doesn't
already have one for that server (hashed entries are recognized).

## Configuration

Each backup directory's `.duplicacy/preferences` file needs a `keys.ssh_key_file`
entry for every sftp destination, and a `keys.password` entry for every encrypted
destination — see the "Reads every destination entry" and RSA-detection bullets
above for how those are used.

`config.yaml` lives at the path shown by:

```bash
duplicacy-backup-runner --help
```

(a [platformdirs](https://pypi.org/project/platformdirs/) user config directory,
e.g. `/root/.config/duplicacy-backup-runner/config.yaml` when run as root under
cron/systemd), or at an explicit path passed with `--config`.

You don't need to create this file yourself: `duplicacy-backup-runner setup`
writes it, setting `healthchecks_uuid`, `client_individual_id`,
`backup_directories`, and, where they apply, `duplicacy_basedir`,
`known_hosts_string` and `filters_url`. Every other setting (`log_basedir`,
`lock_file`, `rate_limit_ip`/`rate_limit_rate`, `log_level`,
`internet_check_*`, `duplicacy_version`, `duplicacy_download_url`) is optional
and can only be set by editing the file by hand after `setup` has run. Use
[`config.example.yaml`](config.example.yaml) as the reference for those keys.

Re-running `setup` keeps any keys you've added, but it rewrites the file, so
comments are not preserved.

Copying `config.example.yaml` into place yourself is only needed if you're
provisioning a host by hand without `setup`, in which case you're also
responsible for everything else `setup` does (see
[First-time host setup](#first-time-host-setup)).

## Usage

```bash
duplicacy-backup-runner [--config PATH] [--log-level LEVEL] [--log-basedir PATH] [-v] [--dry-run]
```

Install a cron job or systemd timer that runs `duplicacy-backup-runner` on your
schedule; it takes out a lock file (`lock_file` in the config) so overlapping runs
are skipped rather than run concurrently.

The systemd unit, cron entry, or Windows scheduled task that `setup` installs
upgrades `duplicacy-backup-runner` from PyPI before each run. It uses
`pipx upgrade` if the tool was installed with pipx on Linux, and the
virtualenv's own `pip` otherwise. If the upgrade fails, for example because
PyPI can't be reached, the backup still runs on the installed version. `setup`
leaves this step out when the tool isn't in a virtualenv, or was installed from
a local path or URL (e.g. `pip install -e .`), since an upgrade would replace
it with the PyPI release. A scheduled run is stopped after 72 hours so a hung
run can't block the next night's run.

`setup` doesn't overwrite a unit, cron entry, or task that already exists. To
pick up changes like this one, delete it and re-run `setup`.

### Dry run

`--dry-run` shows what a run would do without changing anything:

- `duplicacy backup` and `duplicacy prune` run for real with their own `-dry-run`
  option, so you see which files would be uploaded and which snapshots pruned.
- `duplicacy init` and `duplicacy copy`, which have no `-dry-run` option, are
  logged but not run. A destination that would need `init` has its
  backup/copy/prune skipped too, since there's no storage for them to run against yet.
- Provisioning (creating directories, downloading the duplicacy binary or
  filters, chmodding keys) is only logged.
- Output goes to the console only; no log files are written or uploaded, no
  healthchecks.io pings are sent, and no lock is taken.
- Read-only checks still run: sftp connectivity, remote ownership, and
  `duplicacy info` for RSA detection. When `known_hosts_string` is set, the
  sftp checks use a temporary copy generated from it rather than updating
  `<duplicacy_basedir>/keys/known_hosts`, and any difference from the real file
  is still reported.

A dry run needs the duplicacy binary to already be installed. `--dry-run`
isn't supported with `setup`.

## Windows

The tool runs on Windows 10/11 as well. It needs Python and the built-in
OpenSSH Client (`sftp.exe`, under Settings > Optional features). On Windows:

- `duplicacy_basedir` defaults to `C:\duplicacy`, and `config.yaml` lives at
  `C:\duplicacy\config.yaml` rather than in a per-user directory, since
  `setup` runs as an administrator but the scheduled task runs as SYSTEM.
- The default `client_individual_id` is the lowercased computer name.
- Install into a virtualenv that SYSTEM can read and that doesn't depend on
  your profile, e.g.:

  ```powershell
  py -m venv C:\duplicacy\venv
  C:\duplicacy\venv\Scripts\pip install duplicacy-backup-runner
  ```

- Run `C:\duplicacy\venv\Scripts\duplicacy-backup-runner setup` from an
  **elevated** prompt. It:
  - asks you to create directory symlinks in the backup directory with
    `mklink /D` (which also needs an elevated prompt)
  - makes the SSH private key owned by Administrators and readable only by
    SYSTEM and Administrators, which Windows OpenSSH requires
  - registers a `duplicacy-backup-runner` Task Scheduler task that runs
    nightly between 1AM and 4AM as SYSTEM, starts late if the machine was
    off, doesn't start while a previous run is still going, and upgrades
    `duplicacy-backup-runner` with the virtualenv's `pip` before each run.
- RSA encryption is only offered if `openssl` is on the PATH; otherwise
  password encryption is used.

## Development

```bash
python3 -m venv ~/.virtualenvs/duplicacy-backup-runner
source ~/.virtualenvs/duplicacy-backup-runner/bin/activate
pip install -e ".[dev]"
pytest
ruff format .
```

### Integration tests

`tests/test_integration.py` runs a real `duplicacy` binary end-to-end (`init`/`add`/
`backup`/`copy`/`prune`/`info` against throwaway local storage) to verify the CLI
flags and output parsing this tool relies on.

They're skipped unless a binary is found via, in order: `DUPLICACY_TEST_BINARY`,
`tests/bin/duplicacy` (gitignored — drop a binary there), or `duplicacy` on `PATH`.
Get one from [the releases page](https://github.com/gilbertchen/duplicacy/releases).
One test also needs `openssl` on `PATH` and is skipped separately if it's missing.

In an environment with no real internet egress, point `internet_check_url` at a
local server that answers 200 rather than lowering
`internet_check_attempts`/`internet_check_delay` — that exercises the real success
path instead of the "no internet" fallback.

### CI

The **Test** workflow (`.github/workflows/test.yml`) runs on every push to
`main` and every pull request: ruff, plus the unit and integration tests on
Linux against a downloaded `duplicacy` binary.

The **Windows tests** workflow (`.github/workflows/windows.yml`) runs on the
same triggers. It can also be started by hand on any branch, from the Actions
tab or with:

```bash
gh workflow run windows.yml --ref <branch>
```

It runs the unit and integration tests on Windows, then
`tests/test_windows_e2e.py`. That file checks that Windows OpenSSH accepts the
key file permissions `setup` sets, including on a key owned by and only
readable by SYSTEM. It also registers the real scheduled task and runs a backup
through it as SYSTEM. These end-to-end tests change machine-wide state
(`C:\duplicacy`, a scheduled task), so they're skipped unless
`DUPLICACY_BACKUP_RUNNER_WINDOWS_E2E=1` is set, which only that workflow does.
