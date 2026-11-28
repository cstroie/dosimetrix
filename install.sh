#!/usr/bin/env bash
# Dozimetrix installer: checks the requirements, shows what it would do, asks before
# each step, then installs a plain (non-Docker) single-machine deployment, as in
# INSTALL.md / docs/deploy.md.
#
#   sudo ./install.sh              interactive: ask before every step
#   ./install.sh --check           only check requirements and prerequisites (changes nothing)
#   sudo ./install.sh --yes        answer yes to every question (unattended)
#   sudo ./install.sh --dry-run    show every command instead of running it
#
# Options:
#   --no-db         do not create the MariaDB database/user (use an existing one)
#   --no-services   do not install or enable the system services
#   --no-packages   never install system packages (stop if something is missing)
#   --user NAME     service account (default: dozimetrix)
#   --listen ADDR   web UI address for a NEW config (default 127.0.0.1; 0.0.0.0 = every interface)
#   --no-https      no HTTPS proxy in front: a NEW config gets secure_cookies = false, since
#                   browsers drop Secure cookies on http:// and nobody could stay logged in
#
# Safe to run again: existing configuration, secret and database are never overwritten.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_USER=dozimetrix
APP_HOME=/opt/dozimetrix          # code + virtualenv (the unit files expect this path)
CONF_DIR=/etc/dozimetrix
DATA_DIR=/var/lib/dozimetrix
LOG_DIR=/var/log/dozimetrix
CONF="$CONF_DIR/dozimetrix.toml"
DB_NAME=dozimetrix
DB_USER=dozimetrix
WEB_PORT=8104
DICOM_PORT=11112
MIN_PY_MINOR=12                   # Python >= 3.12
MIN_MARIADB="10.6"

CHECK_ONLY=0 ASSUME_YES=0 DRY=0 DO_DB=1 DO_SERVICES=1 DO_PACKAGES=1 NO_HTTPS=0
LISTEN=127.0.0.1
while [[ $# -gt 0 ]]; do
    case "$1" in
        --check) CHECK_ONLY=1 ;;
        -y|--yes) ASSUME_YES=1 ;;
        -n|--dry-run) DRY=1 ;;
        --no-db) DO_DB=0 ;;
        --no-services) DO_SERVICES=0 ;;
        --no-packages) DO_PACKAGES=0 ;;
        --user) APP_USER="${2:?--user needs a name}"; shift ;;
        --listen) LISTEN="${2:?--listen needs an address}"; shift ;;
        --no-https) NO_HTTPS=1 ;;
        -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done

if [[ -t 1 ]]; then B=$'\e[1m' R=$'\e[31m' G=$'\e[32m' Y=$'\e[33m' N=$'\e[0m'; else B= R= G= Y= N=; fi
PROBLEMS=()
ok()   { echo "  ${G}ok${N}      $*"; }
warn() { echo "  ${Y}warning${N} $*"; }
bad()  { echo "  ${R}missing${N} $*"; PROBLEMS+=("$*"); }
step() { echo; echo "${B}== $* ==${N}"; }
die()  { echo "${R}error:${N} $*" >&2; exit 1; }

# Ask a yes/no question (default yes). --yes answers yes.
ask() {
    if (( ASSUME_YES )); then echo "  -> $1 [auto: yes]"; return 0; fi
    [[ -t 0 ]] || die "no terminal to ask '$1' on; run interactively or pass --yes"
    local a; read -r -p "  -> $1 [Y/n] " a
    [[ -z "$a" || "$a" =~ ^[Yy] ]]
}
# Run a command, or only print it with --dry-run.
run() { if (( DRY )); then echo "  [dry-run] $*"; else "$@"; fi; }

SUDO=""
[[ $EUID -eq 0 ]] || SUDO="sudo"

# The config folder is 0750 root:$APP_USER: as another user "-f" says "missing" and the installer
# would write the example over a real config. Ask with the privilege that can see it.
conf_exists() {
    [[ -f "$CONF" ]] && return 0
    [[ $EUID -eq 0 ]] && return 1
    if (( CHECK_ONLY )); then sudo -n test -f "$CONF" 2>/dev/null; else $SUDO test -f "$CONF"; fi
}

ver_ge() { [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" == "$2" ]]; }  # $1 >= $2

find_python() {
    PY=""
    local c
    for c in python3.14 python3.13 python3.12 python3; do
        if command -v "$c" >/dev/null && "$c" -c "import sys; sys.exit(sys.version_info < (3,$MIN_PY_MINOR))" 2>/dev/null; then
            PY=$(command -v "$c"); return 0
        fi
    done
    return 1
}
find_db_client() {
    DB_CLIENT=""
    local c
    for c in mariadb mysql; do command -v "$c" >/dev/null && { DB_CLIENT=$c; return 0; }; done
    return 1
}

# ---------------------------------------------------------------- 1. system
step "1. System"
[[ "$(uname -s)" == Linux ]] || die "Linux only (found $(uname -s))"
ok "Linux $(uname -r)"
if [[ -f /etc/os-release ]]; then . /etc/os-release; ok "${PRETTY_NAME:-unknown distribution}"; fi

PM=""
for p in apt-get dnf apk pacman zypper; do command -v "$p" >/dev/null && { PM=$p; break; }; done
if [[ -n "$PM" ]]; then ok "package manager: $PM"
else warn "no known package manager (apt, dnf, apk, pacman, zypper): packages must be installed by hand"; fi

INIT=none
if [[ -d /run/systemd/system ]]; then INIT=systemd
elif command -v rc-service >/dev/null; then INIT=openrc; fi
ok "init system: $INIT"

[[ -f "$SRC/pyproject.toml" && -d "$SRC/dozimetrix" ]] || die "run install.sh from a Dozimetrix checkout (no pyproject.toml next to it)"
ok "source: $SRC"

probe=$(dirname "$DATA_DIR"); [[ -d $probe ]] || probe=/
free_mb=$(df -Pm "$probe" | awk 'NR==2{print $4}')
if (( free_mb >= 2048 )); then ok "free disk space: ${free_mb} MB"
else warn "only ${free_mb} MB free under $probe (the spool and the database need more; see docs/hardware-requirements.md)"; fi

# ------------------------------------------------------- 2. prerequisites
step "2. Prerequisites"
PKGS=()   # distribution packages the installer can add

if find_python; then
    ok "Python $("$PY" -c 'import platform; print(platform.python_version())') ($PY)"
    if "$PY" -c "import venv, ensurepip" 2>/dev/null; then ok "venv and pip modules"
    else
        bad "Python venv/ensurepip module"
        [[ "$PM" == apt-get ]] && PKGS+=("$(basename "$PY")-venv")
    fi
else
    bad "Python >= 3.$MIN_PY_MINOR"
    case "$PM" in
        apt-get) PKGS+=(python3 python3-venv python3-pip) ;;
        dnf) PKGS+=(python3.12) ;;
        apk) PKGS+=(python3 py3-pip) ;;
        pacman) PKGS+=(python) ;;
        zypper) PKGS+=(python312) ;;
    esac
fi

if find_db_client; then
    dbv=$("$DB_CLIENT" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
    ok "$DB_CLIENT client ${dbv:-?}"
else
    bad "MariaDB client"
    case "$PM" in
        apt-get|zypper|apk) PKGS+=(mariadb-client) ;;
        dnf) PKGS+=(mariadb) ;;
        pacman) PKGS+=(mariadb-clients) ;;
    esac
fi

DB_SERVER_LOCAL=0
if command -v mariadbd >/dev/null || command -v mysqld >/dev/null; then
    DB_SERVER_LOCAL=1
    sv=$( (mariadbd --version || mysqld --version) 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
    if [[ -n "$sv" ]] && ! ver_ge "$sv" "$MIN_MARIADB"; then bad "MariaDB >= $MIN_MARIADB (found $sv)"
    else ok "MariaDB server ${sv:-?}"; fi
elif (( DO_DB )); then
    bad "MariaDB server (needed on this machine; with a remote one use --no-db)"
    case "$PM" in
        apt-get|dnf) PKGS+=(mariadb-server) ;;
        apk|pacman|zypper) PKGS+=(mariadb) ;;
    esac
fi

if command -v tesseract >/dev/null; then ok "tesseract (optional, dose-page OCR)"
else warn "tesseract not found (optional: only for scanners that send the dose as a screenshot)"; OPT_TESS=1; fi

port_busy() { command -v ss >/dev/null && ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1\$"; }
for pp in "$WEB_PORT:web UI" "$DICOM_PORT:DICOM receiver"; do
    if port_busy "${pp%%:*}"; then warn "port ${pp%%:*} (${pp#*:}) is already in use; change it in $CONF"
    else ok "port ${pp%%:*} (${pp#*:}) is free"; fi
done

if [[ -x "$APP_HOME/.venv/bin/dozimetrix" ]]; then warn "already installed in $APP_HOME: this run upgrades it"; fi
if conf_exists; then warn "$CONF exists: it is kept as it is"; fi
if getent passwd "$APP_USER" >/dev/null; then ok "service account $APP_USER exists"; fi

if (( CHECK_ONLY )); then
    echo
    if (( ${#PROBLEMS[@]} )); then
        echo "${Y}${#PROBLEMS[@]} requirement(s) missing:${N}"; printf '  - %s\n' "${PROBLEMS[@]}"
        if (( ${#PKGS[@]} )); then echo "The installer can install: ${PKGS[*]}"; fi
        exit 1
    fi
    echo "${G}All requirements are met.${N}"; exit 0
fi

# ---------------------------------------------------------------- 3. plan
step "3. Plan"
db_line="(skip the database)"; (( DO_DB )) && db_line="create the database '$DB_NAME' and its user, with a random password"
sv_line="(skip the services)"; (( DO_SERVICES )) && sv_line="install the $INIT services (web, receive, qr) and the periodic jobs"
cat <<EOF
  This installer will, asking before each step:
    - install system packages: ${PKGS[*]:-none needed}
    - create the service account '$APP_USER' and the folders $APP_HOME, $CONF_DIR, $DATA_DIR, $LOG_DIR
    - build a virtualenv in $APP_HOME/.venv and install Dozimetrix from $SRC
    - $db_line
    - write $CONF (kept if it exists), then run 'migrate' and 'seed'
    - $sv_line
EOF
(( DRY )) && echo "  ${Y}--dry-run: nothing will be changed${N}"
ask "Continue?" || { echo "Nothing changed."; exit 0; }
if [[ $EUID -ne 0 ]]; then
    command -v sudo >/dev/null || die "run as root (sudo is not installed)"
    echo "  (not root: commands that need it use sudo)"
fi

pkg_install() {
    case "$PM" in
        apt-get) run $SUDO apt-get update -qq; run $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" ;;
        dnf) run $SUDO dnf install -y "$@" ;;
        apk) run $SUDO apk add "$@" ;;
        pacman) run $SUDO pacman -S --needed --noconfirm "$@" ;;
        zypper) run $SUDO zypper --non-interactive install "$@" ;;
    esac
}

# --------------------------------------------------------------- 4. packages
step "4. System packages"
if (( ${#PKGS[@]} )); then
    if (( ! DO_PACKAGES )) || [[ -z "$PM" ]]; then die "missing: ${PKGS[*]}. Install them and run again."; fi
    ask "Install with $PM: ${PKGS[*]}?" || die "cannot continue without: ${PKGS[*]}"
    pkg_install "${PKGS[@]}"
    if (( ! DRY )); then
        find_python || die "Python >= 3.$MIN_PY_MINOR is still not available: install it by hand (pyenv, a PPA, ...)"
        find_db_client || true
    fi
else ok "nothing to install"; fi
PIP_EXTRA=""
if [[ -n "${OPT_TESS:-}" && -n "$PM" ]] && (( DO_PACKAGES )); then
    tp=tesseract-ocr; [[ $PM == dnf || $PM == pacman || $PM == zypper ]] && tp=tesseract
    if ask "Optionally install $tp (OCR of scanner dose pages)?"; then pkg_install "$tp"; PIP_EXTRA="[ocr]"; fi
fi

# ----------------------------------------------------------- 5. account, dirs
step "5. Service account and folders"
if ! getent passwd "$APP_USER" >/dev/null; then
    ask "Create the system user '$APP_USER'?" || die "the service account is required"
    if command -v useradd >/dev/null; then
        run $SUDO useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
    else  # Alpine
        run $SUDO addgroup -S "$APP_USER"
        run $SUDO adduser -S -h "$DATA_DIR" -s /sbin/nologin -G "$APP_USER" "$APP_USER"
    fi
fi
run $SUDO install -d -m 0755 "$APP_HOME"
run $SUDO install -d -m 0750 -o root -g "$APP_USER" "$CONF_DIR"
run $SUDO install -d -m 0700 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR" "$DATA_DIR/spool"
run $SUDO install -d -m 0750 -o "$APP_USER" -g "$APP_USER" "$LOG_DIR"
ok "folders ready"

# ----------------------------------------------------------- 6. application
step "6. Application"
ask "Install Dozimetrix into $APP_HOME/.venv from $SRC?" || die "nothing to do without the application"
[[ -x "$APP_HOME/.venv/bin/python" ]] || run $SUDO "$PY" -m venv "$APP_HOME/.venv"
run $SUDO "$APP_HOME/.venv/bin/pip" install --upgrade pip
if [[ "$(realpath "$SRC")" == "$(realpath "$APP_HOME")" ]]; then
    # the checkout is the install: the code runs from it (update with git pull), the
    # virtualenv only holds the dependencies
    run $SUDO "$APP_HOME/.venv/bin/pip" install --upgrade -e "${SRC}${PIP_EXTRA}"
    # pip ran as root: give its build leftovers back to the checkout's owner (git pull must work)
    run $SUDO rm -rf "$APP_HOME/build"
    if [[ -d "$APP_HOME/dozimetrix.egg-info" ]]; then
        run $SUDO chown -R "$(stat -c %u:%g "$SRC/pyproject.toml")" "$APP_HOME/dozimetrix.egg-info"
    fi
else
    run $SUDO "$APP_HOME/.venv/bin/pip" install --upgrade "${SRC}${PIP_EXTRA}"
fi
# the unit files and the backup scripts expect $APP_HOME/deploy
# (skip when installing from the checkout that already is $APP_HOME: rm would delete the source)
if [[ "$(realpath "$SRC")" != "$(realpath "$APP_HOME")" ]]; then
    run $SUDO rm -rf "$APP_HOME/deploy"
    run $SUDO cp -r "$SRC/deploy" "$APP_HOME/deploy"
fi
ok "application installed"
DOZ=("$APP_HOME/.venv/bin/dozimetrix" --config "$CONF")
as_app() {  # run a command as the service account
    if (( DRY )); then echo "  [dry-run] as $APP_USER: $*"
    elif command -v runuser >/dev/null && [[ $EUID -eq 0 ]]; then runuser -u "$APP_USER" -- "$@"
    else sudo -u "$APP_USER" "$@"; fi
}

# ------------------------------------------------------------ 7. database
DB_PASS=""
if (( DO_DB )) && ! conf_exists; then
    step "7. Database"
    if [[ -z "${DB_CLIENT:-}" ]] && (( ! DRY )); then
        warn "no MariaDB client: create the database by hand (INSTALL.md) and edit $CONF"
    elif ask "Create database '$DB_NAME' and user '$DB_USER'@'localhost' (random password)?"; then
        if (( DRY )); then
            echo "  [dry-run] $DB_CLIENT: CREATE DATABASE, CREATE USER, GRANT"; DB_PASS=dry-run-password
        else
            if ! $SUDO "$DB_CLIENT" -e 'SELECT 1' >/dev/null 2>&1; then   # not running yet?
                case "$INIT" in
                    systemd) $SUDO systemctl enable --now mariadb 2>/dev/null || $SUDO systemctl enable --now mysql 2>/dev/null || true ;;
                    openrc) $SUDO rc-service mariadb start 2>/dev/null || true ;;
                esac
                sleep 3
            fi
            if ! $SUDO "$DB_CLIENT" -e 'SELECT 1' >/dev/null 2>&1; then
                warn "cannot connect to MariaDB as root (is it running? does root need a password?)"
                warn "create the database by hand (INSTALL.md) and edit $CONF"
            elif $SUDO "$DB_CLIENT" -N -e "SHOW DATABASES LIKE '$DB_NAME'" | grep -q .; then
                warn "database '$DB_NAME' already exists: left as it is. Put its URL in $CONF"
            else
                DB_PASS=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
                # the password goes through stdin, never on a command line
                $SUDO "$DB_CLIENT" <<SQL
CREATE DATABASE \`$DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS';
GRANT ALL ON \`$DB_NAME\`.* TO '$DB_USER'@'localhost';
SQL
                ok "database and user created"
                warn "give InnoDB memory for the data: innodb_buffer_pool_size (docs/deploy.md, section 2)"
            fi
        fi
    fi
fi

# ------------------------------------------------------------ 8. config
step "8. Configuration"
if conf_exists; then ok "$CONF kept"
elif ask "Write $CONF from the example?"; then
    tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
    cp "$SRC/deploy/dozimetrix.toml.example" "$tmp"
    if [[ -n "$DB_PASS" ]]; then sed -i "s|CHANGE-ME|$DB_PASS|" "$tmp"
    else warn "no database password set: edit the [database] url in $CONF"; fi
    if [[ "$LISTEN" != 127.0.0.1 ]]; then sed -i "s|^host = \"127\.0\.0\.1\"|host = \"$LISTEN\"|" "$tmp"; fi
    if (( NO_HTTPS )); then
        sed -i 's|^secure_cookies = true|secure_cookies = false|' "$tmp"
        warn "secure_cookies = false: session cookies travel unencrypted over http://. Fine on a trusted LAN or for the first setup; put an HTTPS proxy in front and set it back to true"
    elif [[ "$LISTEN" != 127.0.0.1 && "$LISTEN" != localhost ]]; then
        warn "listening on $LISTEN with secure_cookies = true: logins only work through an HTTPS proxy (browsers drop Secure cookies on http://). Without one, run again with --no-https or set secure_cookies = false in $CONF"
    fi
    run $SUDO install -m 0640 -o root -g "$APP_USER" "$tmp" "$CONF"
    warn "edit [pacs], [dicom] and [notify] in $CONF for your site"
fi
if conf_exists && { [[ "$LISTEN" != 127.0.0.1 ]] || (( NO_HTTPS )); } && [[ -z "${tmp:-}" ]]; then
    warn "$CONF already existed: --listen / --no-https were not applied. Set [web] host and [security] secure_cookies there"
fi

# a placeholder password means the database step did not finish: do not start services on it
DB_OK=1
if (( ! DRY )) && conf_exists && $SUDO grep -qE '^url *=.*CHANGE-ME' "$CONF"; then
    DB_OK=0
    warn "the [database] url in $CONF still has the placeholder password CHANGE-ME"
    warn "create the database and user (INSTALL.md, 'by hand'), put the real url there, run install.sh again"
fi

# ----------------------------------------------------- 9. schema and seeds
step "9. Schema and seed data"
if (( ! DB_OK )); then warn "skipped: the database is not set up (see above)"
elif conf_exists || (( DRY )); then
    if ask "Run 'migrate' and 'seed'?"; then
        if as_app "${DOZ[@]}" migrate && as_app "${DOZ[@]}" seed; then ok "database ready"
        else
            DB_OK=0
            warn "migrate/seed failed: check the [database] url in $CONF (user, password, database)"
        fi
    fi
else DB_OK=0; warn "no $CONF: migrate and seed skipped"; fi

# ------------------------------------------------------------- 10. services
SERVICES_STARTED=0
if (( DO_SERVICES )) && (( ! DB_OK )); then
    step "10. Services ($INIT)"
    warn "not installed: they would crash on the database. Fix the above, then run install.sh again"
elif (( DO_SERVICES )) && [[ "$INIT" != none ]]; then
    step "10. Services ($INIT)"
    if ask "Install and enable the $INIT services (web, receive + periodic jobs)?"; then
        if [[ "$INIT" == systemd ]]; then
            run $SUDO cp "$SRC"/deploy/systemd/*.service "$SRC"/deploy/systemd/*.timer /etc/systemd/system/
            run $SUDO systemctl daemon-reload
            run $SUDO systemctl enable --now dozimetrix-web dozimetrix-receive
            SERVICES_STARTED=1
            if ask "Also start dozimetrix-qr? (it queries the PACS: set [pacs] in $CONF first)"; then
                run $SUDO systemctl enable --now dozimetrix-qr
            fi
            run $SUDO systemctl enable --now dozimetrix-maintain.timer dozimetrix-backup.timer dozimetrix-backup-check.timer
        else
            run $SUDO install -m 0755 "$SRC/deploy/openrc/dozimetrix.initd" /etc/init.d/dozimetrix
            for d in web receive his qr; do run $SUDO ln -sf dozimetrix "/etc/init.d/dozimetrix.$d"; done
            [[ -f /etc/conf.d/dozimetrix ]] || run $SUDO install -m 0644 "$SRC/deploy/openrc/dozimetrix.confd" /etc/conf.d/dozimetrix
            for d in web receive; do run $SUDO rc-update add "dozimetrix.$d" default; run $SUDO rc-service "dozimetrix.$d" start; done
            SERVICES_STARTED=1
            warn "periodic jobs run from cron: install cronie, then copy deploy/cron/dozimetrix to /etc/cron.d/"
        fi
    fi
elif (( DO_SERVICES )); then
    warn "no systemd or OpenRC: start the daemons with $APP_HOME/deploy/dozimetrix-ctl start all"
fi

# --------------------------------------------------------------- done
step "Done"
# the web service writes the one-time setup PIN when it starts: show it here
SETUP_PIN=""
if (( ! DRY && ! CHECK_ONLY )); then
    web_up() { case "$INIT" in
        systemd) systemctl is-active --quiet dozimetrix-web ;;
        openrc) rc-service dozimetrix.web status >/dev/null 2>&1 ;;
        *) return 1 ;; esac; }
    if web_up; then
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            SETUP_PIN=$($SUDO cat "$DATA_DIR/setup-token" 2>/dev/null || true)
            [[ -n "$SETUP_PIN" ]] && break
            sleep 1
        done
    fi
fi
# does the web UI answer? (a service that is "active" can still be restarting in a crash loop)
if (( SERVICES_STARTED && ! DRY )) && command -v curl >/dev/null; then
    code=000
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
        code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://127.0.0.1:$WEB_PORT/login" || true)
        [[ "$code" =~ ^[23] ]] && break
        sleep 1
    done
    if [[ "$code" =~ ^[23] ]]; then ok "the web UI answers on port $WEB_PORT"
    else
        DB_OK=0
        warn "the web UI does not answer on port $WEB_PORT (last status $code): journalctl -u dozimetrix-web -n 30"
    fi
fi
if [[ -n "$SETUP_PIN" ]]; then
    echo "  ${B}First-run setup PIN: $SETUP_PIN${N}   (a new one is issued if the web service restarts)"
fi
SHOW_HOST=$LISTEN; [[ "$LISTEN" == 0.0.0.0 ]] && SHOW_HOST="<this machine's address>"
cat <<EOF
  Open  http://${SHOW_HOST}:$WEB_PORT/setup  to finish with the first-run wizard (host and port: [web] in $CONF).
  The one-time setup PIN is in the web service log and in $DATA_DIR/setup-token.
  Back up $DATA_DIR/secret once it exists: pseudonyms and sessions depend on it.
  Put an HTTPS reverse proxy (nginx, Apache) in front of the web UI; keep secure_cookies = true there
  (over plain http:// logins need secure_cookies = false: see INSTALL.md, "Remote access").
  DICOM: register AE DOZIMETRIX / this host / $DICOM_PORT on the modalities and the PACS.
EOF
(( DB_OK )) || { echo "${Y}Installation incomplete: see the warnings above.${N}"; exit 1; }
