#!/bin/bash
set -euo pipefail

# ============================================================================
# Evidence-Capture Session Controller — Full System Setup
#
# Run:  bash setup.sh
#
# This script sets up everything needed on a fresh Ubuntu system:
#   1. System packages (ffmpeg, tshark, exiftool, etc.)
#   2. v4l2loopback kernel module + passwordless sudo
#   3. Python venvs (main + mitmproxy)
#   4. mitmproxy CA certificate
#   5. Firefox profile configured for mitmproxy
#   6. OBS WebSocket setup reminder
#   7. Signing key setup
#   8. Environment file (.env)
#
# Safe to re-run — skips steps that are already done.
# ============================================================================

# ---- Do NOT run this script with sudo. It calls sudo internally when needed.
if [ "$(id -u)" = "0" ]; then
    echo ""
    echo "  ERROR: Do not run this script as root / with sudo."
    echo "         It creates venvs and configs under YOUR home directory,"
    echo "         and calls sudo internally for the steps that need it."
    echo ""
    echo "  Run:   bash setup.sh"
    echo ""
    exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(whoami)"
VENV_MAIN="$HOME/.venvs/evidence-capture"
VENV_MITM="$HOME/.venvs/mitm"
FFOX_PROFILE="$HOME/.firefox-mitmproxy"
MITM_CA_DIR="$HOME/.mitmproxy"
ENV_FILE="$REPO_DIR/.env"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

step=0
total_steps=8

header() {
    step=$((step + 1))
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}  Step $step/$total_steps: $1${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

ok()   { echo -e "  ${GREEN}OK${NC}  $1"; }
skip() { echo -e "  ${YELLOW}SKIP${NC}  $1 (already done)"; }
warn() { echo -e "  ${YELLOW}WARN${NC}  $1"; }
fail() { echo -e "  ${RED}FAIL${NC}  $1"; }

ask_continue() {
    echo ""
    read -p "  Continue? [Y/n] " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Nn]$ ]]; then
        echo "Skipping this step."
        return 1
    fi
    return 0
}

# ============================================================================
# STEP 1: System packages
# ============================================================================
header "System packages (apt)"

PKGS_NEEDED=""
for pkg in ffmpeg tshark exiftool pdfsig; do
    if ! command -v "$pkg" &>/dev/null; then
        case $pkg in
            exiftool) PKGS_NEEDED="$PKGS_NEEDED libimage-exiftool-perl" ;;
            pdfsig)   PKGS_NEEDED="$PKGS_NEEDED poppler-utils" ;;
            tshark)   PKGS_NEEDED="$PKGS_NEEDED tshark" ;;
            *)        PKGS_NEEDED="$PKGS_NEEDED $pkg" ;;
        esac
    fi
done

# Also need these for building Python packages and v4l2loopback
for pkg in python3-venv python3.10-venv python3-dev build-essential v4l2loopback-dkms v4l-utils fonts-liberation acl tesseract-ocr libnss3-tools; do
    if ! dpkg -l "$pkg" &>/dev/null 2>&1; then
        PKGS_NEEDED="$PKGS_NEEDED $pkg"
    fi
done

# PyQt5 system package (much easier than pip on Ubuntu)
if ! python3 -c "import PyQt5" &>/dev/null 2>&1; then
    PKGS_NEEDED="$PKGS_NEEDED python3-pyqt5"
fi

if [ -n "$PKGS_NEEDED" ]; then
    echo "  Need to install:$PKGS_NEEDED"
    if ask_continue; then
        sudo apt update
        sudo apt install -y $PKGS_NEEDED
        ok "System packages installed"
    fi
else
    skip "All system packages present"
fi

# websocat (not in apt)
if ! command -v websocat &>/dev/null; then
    echo ""
    echo "  websocat is needed for the network stream viewer."
    echo "  Install options:"
    echo "    snap install websocat"
    echo "    cargo install websocat"
    echo "    Or download binary:"
    echo "      curl -L -o /tmp/websocat https://github.com/vi/websocat/releases/latest/download/websocat.x86_64-unknown-linux-musl"
    echo "      chmod +x /tmp/websocat && sudo mv /tmp/websocat /usr/local/bin/websocat"
    echo ""
    if ask_continue; then
        ARCH=$(uname -m)
        if [ "$ARCH" = "x86_64" ]; then
            curl -L -o /tmp/websocat "https://github.com/vi/websocat/releases/latest/download/websocat.x86_64-unknown-linux-musl"
            chmod +x /tmp/websocat
            sudo mv /tmp/websocat /usr/local/bin/websocat
            ok "websocat installed to /usr/local/bin"
        else
            warn "Please install websocat manually for arch: $ARCH"
        fi
    fi
else
    skip "websocat already installed"
fi

# ============================================================================
# STEP 2: v4l2loopback + passwordless sudo
# ============================================================================
header "v4l2loopback kernel module + sudo access"

# Create the setup script
VCAM_SCRIPT="/usr/local/sbin/obs_vcam_setup.sh"
echo "  This creates $VCAM_SCRIPT and allows passwordless sudo for it."
echo "  The script loads the v4l2loopback kernel module for OBS virtual camera."

if ask_continue; then
    # Write the vcam setup script
    sudo tee "$VCAM_SCRIPT" > /dev/null <<VCAM
#!/bin/bash
/usr/sbin/modprobe -r v4l2loopback 2>/dev/null || true
/usr/sbin/modprobe v4l2loopback devices=1 video_nr=2 card_label="OBS-Virtual" exclusive_caps=1
sleep 0.5
/usr/bin/setfacl -m u:${USER_NAME}:rw /dev/video2 2>/dev/null || true
echo "v4l2loopback loaded: /dev/video2 (OBS-Virtual)"
VCAM
    sudo chmod 755 "$VCAM_SCRIPT"

    # Add passwordless sudo rule
    SUDOERS_FILE="/etc/sudoers.d/evidence-capture-vcam"
    if [ ! -f "$SUDOERS_FILE" ]; then
        echo "${USER_NAME} ALL=(root) NOPASSWD: $VCAM_SCRIPT" | sudo tee "$SUDOERS_FILE" > /dev/null
        sudo chmod 440 "$SUDOERS_FILE"
        ok "Passwordless sudo configured for vcam setup"
    else
        skip "Sudoers rule already exists"
    fi

    # Load the module now
    sudo "$VCAM_SCRIPT"
    ok "v4l2loopback loaded"
else
    warn "Skipped — vcam won't work without this"
fi

# ============================================================================
# STEP 3: Python virtual environments
# ============================================================================
header "Python virtual environments"

# Main venv
if [ ! -d "$VENV_MAIN" ]; then
    echo "  Creating main venv at $VENV_MAIN"
    python3 -m venv "$VENV_MAIN" --system-site-packages
    source "$VENV_MAIN/bin/activate"
    pip install --upgrade pip
    pip install -r "$REPO_DIR/requirements.txt"
    deactivate
    ok "Main venv created and packages installed"
else
    skip "Main venv exists at $VENV_MAIN"
    # Still make sure packages are up to date
    source "$VENV_MAIN/bin/activate"
    pip install -r "$REPO_DIR/requirements.txt" -q 2>/dev/null
    deactivate
fi

# Mitmproxy venv (separate because it pins conflicting deps)
if [ ! -d "$VENV_MITM" ]; then
    echo "  Creating mitmproxy venv at $VENV_MITM"
    python3 -m venv "$VENV_MITM"
    source "$VENV_MITM/bin/activate"
    pip install --upgrade pip
    pip install mitmproxy
    deactivate
    ok "Mitmproxy venv created"
else
    skip "Mitmproxy venv exists at $VENV_MITM"
fi

# ============================================================================
# STEP 4: mitmproxy CA certificate
# ============================================================================
header "mitmproxy CA certificate generation"

if [ -f "$MITM_CA_DIR/mitmproxy-ca-cert.pem" ]; then
    skip "mitmproxy CA cert already exists at $MITM_CA_DIR"
else
    echo "  Starting mitmproxy briefly to generate its CA certificate..."
    echo "  (This creates ~/.mitmproxy/mitmproxy-ca-cert.pem)"

    source "$VENV_MITM/bin/activate"
    # Run mitmdump for 2 seconds to generate certs, then kill it
    timeout 3 mitmdump --listen-port 18080 &>/dev/null || true
    deactivate

    if [ -f "$MITM_CA_DIR/mitmproxy-ca-cert.pem" ]; then
        ok "CA certificate generated at $MITM_CA_DIR/mitmproxy-ca-cert.pem"
    else
        fail "CA cert not generated — you may need to run mitmdump manually once"
    fi
fi

# ============================================================================
# STEP 5: Firefox profile for mitmproxy
# ============================================================================
header "Firefox profile for mitmproxy"

if [ -d "$FFOX_PROFILE" ]; then
    skip "Firefox profile exists at $FFOX_PROFILE"
else
    echo "  Creating a new Firefox profile at $FFOX_PROFILE"
    echo "  This profile will:"
    echo "    - Route all traffic through mitmproxy (127.0.0.1:18080)"
    echo "    - Trust the mitmproxy CA certificate"
    echo "    - Use ~/downloads as the default download folder"

    if ask_continue; then
        # Create the profile directory
        mkdir -p "$FFOX_PROFILE"

        # Create user.js with proxy and download settings
        cat > "$FFOX_PROFILE/user.js" <<'USERJS'
// === Evidence-Capture Firefox Profile ===
// Proxy: route through mitmproxy
user_pref("network.proxy.type", 1);
user_pref("network.proxy.http", "127.0.0.1");
user_pref("network.proxy.http_port", 18080);
user_pref("network.proxy.ssl", "127.0.0.1");
user_pref("network.proxy.ssl_port", 18080);
user_pref("network.proxy.no_proxies_on", "localhost, 127.0.0.1");

// Downloads: save to ~/downloads (no ask)
user_pref("browser.download.dir", "/home/USERPLACEHOLDER/downloads");
user_pref("browser.download.folderList", 2);
user_pref("browser.download.useDownloadDir", true);

// Disable auto-updates (we want a stable capture environment)
user_pref("app.update.enabled", false);
user_pref("app.update.auto", false);

// Disable telemetry
user_pref("toolkit.telemetry.enabled", false);
user_pref("datareporting.healthreport.uploadEnabled", false);
USERJS
        # Replace placeholder with actual username
        sed -i "s|USERPLACEHOLDER|${USER_NAME}|g" "$FFOX_PROFILE/user.js"

        # Create the downloads directory
        mkdir -p "$HOME/downloads"

        ok "Firefox profile created at $FFOX_PROFILE"

        # Import mitmproxy CA cert into Firefox's cert store
        if [ -f "$MITM_CA_DIR/mitmproxy-ca-cert.pem" ]; then
            echo ""
            echo "  Now importing mitmproxy CA certificate into Firefox..."
            echo ""

            # Check if certutil is available
            if command -v certutil &>/dev/null; then
                # Create cert9.db if it doesn't exist
                if [ ! -f "$FFOX_PROFILE/cert9.db" ]; then
                    # Initialize the NSS database
                    certutil -d "sql:$FFOX_PROFILE" -N --empty-password
                fi
                # Import the CA cert as trusted
                certutil -d "sql:$FFOX_PROFILE" -A \
                    -n "mitmproxy CA" \
                    -t "TC,TC,TC" \
                    -i "$MITM_CA_DIR/mitmproxy-ca-cert.pem"
                ok "mitmproxy CA cert imported into Firefox profile"
            else
                warn "certutil not found. Installing libnss3-tools..."
                sudo apt install -y libnss3-tools
                if [ ! -f "$FFOX_PROFILE/cert9.db" ]; then
                    certutil -d "sql:$FFOX_PROFILE" -N --empty-password
                fi
                certutil -d "sql:$FFOX_PROFILE" -A \
                    -n "mitmproxy CA" \
                    -t "TC,TC,TC" \
                    -i "$MITM_CA_DIR/mitmproxy-ca-cert.pem"
                ok "mitmproxy CA cert imported into Firefox profile"
            fi
        else
            warn "mitmproxy CA cert not found — you'll need to import it manually"
            echo "       Open Firefox with this profile, visit http://mitm.it, and install the cert"
        fi
    fi
fi

# ============================================================================
# STEP 6: OBS WebSocket setup
# ============================================================================
header "OBS Studio WebSocket"

echo "  OBS WebSocket needs to be enabled manually in OBS:"
echo ""
echo "    1. Open OBS Studio"
echo "    2. Tools -> WebSocket Server Settings"
echo "    3. Enable WebSocket server"
echo "    4. Set port to 4455 (default)"
echo "    5. Set password (or leave blank)"
echo "    6. Click OK"
echo ""
echo "  Also set up your scene:"
echo "    - Add a Window Capture source pointing at the Firefox window"
echo "    - Add a Video Capture Device (V4L2) source -> select 'OBS-Virtual' (/dev/video2)"
echo "    - Position the hash overlay at the bottom of your scene"
echo ""
ok "OBS instructions noted (manual step)"

# ============================================================================
# STEP 7: PDF signing keys
# ============================================================================
header "PDF signing credentials"

if [ -n "${EVCAP_KEY_PEM:-}" ] && [ -f "${EVCAP_KEY_PEM:-}" ]; then
    skip "EVCAP_KEY_PEM is set and file exists"
elif [ -n "${EVCAP_P12:-}" ] && [ -f "${EVCAP_P12:-}" ]; then
    skip "EVCAP_P12 is set and file exists"
else
    echo "  No signing credentials found. You need either:"
    echo ""
    echo "  Option A — Self-signed cert (quick, fine for personal use):"
    echo ""
    echo "    openssl req -x509 -newkey rsa:2048 -nodes \\"
    echo "      -keyout ~/evidence-key.pem \\"
    echo "      -out ~/evidence-cert.pem \\"
    echo "      -days 3650 \\"
    echo "      -subj '/CN=Evidence Capture/O=Personal'"
    echo ""
    echo "  Then set in .env:"
    echo "    EVCAP_KEY_PEM=~/evidence-key.pem"
    echo "    EVCAP_CERT_PEM=~/evidence-cert.pem"
    echo ""

    read -p "  Generate a self-signed cert now? [y/N] " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        KEY_PATH="$HOME/evidence-key.pem"
        CERT_PATH="$HOME/evidence-cert.pem"
        openssl req -x509 -newkey rsa:2048 -nodes \
            -keyout "$KEY_PATH" \
            -out "$CERT_PATH" \
            -days 3650 \
            -subj "/CN=Evidence Capture/O=Personal" 2>/dev/null
        chmod 600 "$KEY_PATH"
        ok "Self-signed cert created: $KEY_PATH + $CERT_PATH"
    else
        warn "No signing keys — download watcher PDF signing will fail"
    fi
fi

# ============================================================================
# STEP 8: Generate .env file
# ============================================================================
header "Environment file (.env)"

KEY_PATH="${EVCAP_KEY_PEM:-$HOME/evidence-key.pem}"
CERT_PATH="${EVCAP_CERT_PEM:-$HOME/evidence-cert.pem}"

if [ -f "$ENV_FILE" ]; then
    echo "  .env already exists at $ENV_FILE"
    read -p "  Overwrite? [y/N] " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        skip "Keeping existing .env"
        # Jump to summary
        ENV_WRITTEN=0
    else
        ENV_WRITTEN=1
    fi
else
    ENV_WRITTEN=1
fi

if [ "${ENV_WRITTEN:-1}" = "1" ]; then
    cat > "$ENV_FILE" <<ENVFILE
# Evidence-Capture — generated by setup.sh on $(date -I)

# Python interpreters
export EVCAP_PY=$VENV_MAIN/bin/python
export EVCAP_MITM_PY=$VENV_MITM/bin/python
export EVCAP_MITMDUMP=$VENV_MITM/bin/mitmdump

# Firefox
export EVCAP_FFOX=$(which firefox)
export EVCAP_FFOX_PROFILE=$FFOX_PROFILE

# PDF signing
export EVCAP_KEY_PEM=$KEY_PATH
export EVCAP_CERT_PEM=$CERT_PATH
export EVCAP_SIGN_CONTACT=
export EVCAP_SIGN_LOCATION=
export EVCAP_SIGN_REASON=Digitally signed

# OBS
export OBS_HOST=127.0.0.1
export OBS_PORT=4455
export OBS_PASSWORD=
ENVFILE
    ok "Wrote $ENV_FILE"
fi

# ============================================================================
# SUMMARY
# ============================================================================
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Setup complete!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  To start using the system:"
echo ""
echo "    # 1. Source your environment"
echo "    source $ENV_FILE"
echo ""
echo "    # 2. Activate the main venv"
echo "    source $VENV_MAIN/bin/activate"
echo ""
echo "    # 3. Verify everything is ready"
echo "    python3 bin/check_deps.py"
echo ""
echo "    # 4. Launch the controller"
echo "    python3 bin/control_all.py interactive"
echo ""
echo "  Quick reference (inside the controller):"
echo "    vcam          - enable OBS virtual camera"
echo "    hash          - show hash overlay window"
echo "    mitm          - start mitmproxy"
echo "    ffox          - launch Firefox (mitmproxy profile)"
echo "    start_session \"My Session\" 10.0"
echo "    stop_session"
echo ""
echo "  Manual steps still needed:"
echo "    - Configure OBS WebSocket (see Step 6 above)"
echo "    - Set up OBS scene with Firefox window + hash overlay"
echo "    - Fill in EVCAP_SIGN_CONTACT/LOCATION in .env (optional)"
echo ""
