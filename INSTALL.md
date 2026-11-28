# Installing Dozimetrix

A plain installation on one Linux machine: MariaDB, the web UI/API, the DICOM receiver and the
PACS query/retrieve job. `install.sh` does all of it, asking before each step; this page lists
what it checks and does, and the manual equivalent. More detail (OpenRC, reverse proxy, backups,
maintenance) is in [docs/deploy.md](docs/deploy.md); sizing is in
[docs/hardware-requirements.md](docs/hardware-requirements.md).

## Quick start

```sh
git clone https://github.com/cstroie/dozimetrix.git && cd dozimetrix
./install.sh --check        # requirements only: changes nothing
sudo ./install.sh           # install, with a question before every step
```

Then open `http://127.0.0.1:8104/setup`. The first-run wizard asks for the one-time setup token (a 4-digit PIN, shown at the end of `install.sh`)
(in the web service log and in `/var/lib/dozimetrix/setup-token`), creates the first
administrator and configures the site and the PACS node.

## Requirements

| What | Needed | Checked by the script |
|---|---|---|
| OS | Linux, systemd or OpenRC (or none: `deploy/dozimetrix-ctl`) | yes |
| Python | ≥ 3.12 with `venv` and `pip` | yes; installed from the distribution if missing |
| MariaDB | ≥ 10.6 server and client (MySQL-compatible) | yes; installed if missing |
| Disk | a few GB free for the spool and the database | yes (warns under 2 GB) |
| Ports | 8104 (web, put HTTPS in front), 11112 (DICOM) | yes (warns if busy) |
| tesseract | optional: scanners that send the dose as a screenshot | yes; offered |
| Privileges | root or `sudo` | yes |

Package installation covers apt, dnf, apk, pacman and zypper. If your distribution ships an older
Python, install 3.12 yourself (pyenv, a PPA, a container) and run the script again; it picks
`python3.12` or newer from `PATH`.

## What the installer does

Each step asks first (`--yes` answers all; `--dry-run` prints the commands instead):

1. Installs the missing system packages.
2. Creates the system user `dozimetrix` and the folders `/opt/dozimetrix` (code, virtualenv),
   `/etc/dozimetrix` (configuration, `0750 root:dozimetrix`), `/var/lib/dozimetrix` (secret,
   spool; `0700`) and `/var/log/dozimetrix`.
3. Installs the application into `/opt/dozimetrix/.venv` and copies `deploy/` next to it (the
   unit files and backup scripts use it).
4. Creates the database `dozimetrix` and its user with a random password (through the MariaDB
   root socket; the password never appears on a command line).
5. Writes `/etc/dozimetrix/dozimetrix.toml` from `deploy/dozimetrix.toml.example`, with that
   password. An existing file is never overwritten.
6. Runs `dozimetrix migrate` and `dozimetrix seed`.
7. Installs and starts the services (web, receive, optionally qr) and the maintenance and backup
   timers (systemd), or the init scripts (OpenRC).

It can be run again to upgrade: the virtualenv and `deploy/` are refreshed, the configuration,
the database and the secret are left alone.

**Checkout in `/opt/dozimetrix` (recommended).** `git clone <repo> /opt/dozimetrix` and run
`install.sh` from there: the code then runs straight from the checkout and `.venv` only holds the
Python packages (an editable install). To update:

```sh
cd /opt/dozimetrix && git pull
.venv/bin/pip install -e .            # only when pyproject.toml's dependencies changed
sudo -u dozimetrix .venv/bin/dozimetrix migrate && sudo -u dozimetrix .venv/bin/dozimetrix seed
sudo systemctl restart dozimetrix-web dozimetrix-receive      # and dozimetrix-qr / -his if used
```

Do not leave the checkout on a feature branch or with uncommitted edits: the services run
whatever is in the working tree. Run `git pull` as your own user, not with `sudo`, or git's
files become root-owned. From a checkout elsewhere, `install.sh` does a normal (copying) install
instead, and an update means running it again.

Options: `--no-db` (use an existing or remote database: edit the `url` in the toml),
`--no-services`, `--no-packages`, `--user NAME`. Paths are fixed because the unit files use them.

## After the installation

- Edit `[pacs]`, `[dicom]` and `[notify]` in `/etc/dozimetrix/dozimetrix.toml`, then restart the
  services. Start `dozimetrix-qr` only once `[pacs]` is right.
- **Back up `/var/lib/dozimetrix/secret`**: pseudonyms and sessions depend on it.
- Publish the web UI through an HTTPS reverse proxy and keep `secure_cookies = true`.
- Give InnoDB enough memory for the data (`innodb_buffer_pool_size`, about the size of the data).
- Register the AE `DOZIMETRIX` (this host, port 11112) on the modalities and the PACS that send dose objects.
- A HIS/RIS server: `systemctl enable --now dozimetrix-his` (see docs/deploy.md).

## Manual installation

```sh
sudo useradd --system --home-dir /var/lib/dozimetrix --shell /usr/sbin/nologin dozimetrix
sudo install -d -m 0750 -o root -g dozimetrix /etc/dozimetrix
sudo install -d -m 0700 -o dozimetrix -g dozimetrix /var/lib/dozimetrix /var/lib/dozimetrix/spool
sudo python3.12 -m venv /opt/dozimetrix/.venv
sudo /opt/dozimetrix/.venv/bin/pip install .          # from a checkout elsewhere; with the checkout in /opt/dozimetrix: pip install -e .
sudo cp -r deploy /opt/dozimetrix/deploy
```
```sql
CREATE DATABASE dozimetrix CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'dozimetrix'@'localhost' IDENTIFIED BY '…';
GRANT ALL ON dozimetrix.* TO 'dozimetrix'@'localhost';
```
```sh
sudo install -m 0640 -o root -g dozimetrix deploy/dozimetrix.toml.example /etc/dozimetrix/dozimetrix.toml  # edit
sudo -u dozimetrix /opt/dozimetrix/.venv/bin/dozimetrix --config /etc/dozimetrix/dozimetrix.toml migrate
sudo -u dozimetrix /opt/dozimetrix/.venv/bin/dozimetrix --config /etc/dozimetrix/dozimetrix.toml seed
sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now dozimetrix-web dozimetrix-receive
```

## Docker

`Dockerfile` and `docker-compose.yml` are in the repository root; see `deploy/docker/`.

## Uninstalling

```sh
sudo systemctl disable --now dozimetrix-web dozimetrix-receive dozimetrix-qr dozimetrix-his
sudo rm /etc/systemd/system/dozimetrix-* && sudo systemctl daemon-reload
sudo rm -rf /opt/dozimetrix
```
`/etc/dozimetrix`, `/var/lib/dozimetrix` (including the secret) and the database are left for you
to remove or keep; the installer never deletes them.
