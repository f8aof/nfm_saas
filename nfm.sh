#!/bin/bash
# =============================================================================
#  NFM SaaS — Installation + lancement local (Linux Mint 22.3)
#  Lance le serveur sur http://localhost:5000
# =============================================================================
set -euo pipefail

G='\033[0;32m'; C='\033[0;36m'; A='\033[0;33m'; R='\033[0;31m'
B='\033[1m'; N='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$SCRIPT_DIR/.installed"

banner() {
  echo -e "${C}${B}"
  echo "  ╔══════════════════════════════════════════════╗"
  echo "  ║   NFM SaaS Platform — F8AOF                 ║"
  echo "  ║   Noise Floor Meter · Multi-utilisateurs    ║"
  echo "  ╚══════════════════════════════════════════════╝"
  echo -e "${N}"
}

install_server() {
  echo -e "${A}> Installation dependances systeme (ALSA direct)...${N}"
  sudo apt-get update -qq
  sudo apt-get install -y \
    python3-pip python3-venv \
    libasound2-dev libasound2-plugins \
    portaudio19-dev \
    python3-dev pkg-config \
    alsa-utils > /dev/null 2>&1
  echo -e "${G}  OK Systeme + ALSA${N}"

  # Groupe audio pour acces ALSA direct sans sudo
  if ! groups "$USER" | grep -qw audio; then
    echo -e "${A}  Ajout de $USER au groupe audio...${N}"
    sudo usermod -aG audio "$USER"
    echo -e "${A}  IMPORTANT : deconnectez/reconnectez votre session pour que le groupe audio soit actif${N}"
  else
    echo -e "${G}  OK Groupe audio deja configure${N}"
  fi

  # Verifier que l EMU 0202 est visible par ALSA
  echo -e "${A}> Detection carte son ALSA...${N}"
  if aplay -l 2>/dev/null | grep -qi "emu\|0202\|usb audio"; then
    echo -e "${G}  OK EMU 0202 / USB Audio detectee :${N}"
    aplay -l 2>/dev/null | grep -i "emu\|0202\|usb" | sed "s/^/    /"
  else
    echo -e "${A}  Cartes son ALSA disponibles :${N}"
    cat /proc/asound/cards 2>/dev/null | sed "s/^/    /" || echo "    Aucune"
    echo -e "${A}  Verifiez la connexion USB de l EMU 0202${N}"
  fi

  # Verifier le mode ALSA (pas PulseAudio)
  if pactl info 2>/dev/null | grep -q "PulseAudio\|PipeWire"; then
    echo -e "${A}  ATTENTION : PulseAudio/PipeWire detecte${N}"
    echo -e "${A}  L agent utilisera quand meme ALSA direct (PA_ALSA_PLUGHW=1)${N}"
  else
    echo -e "${G}  OK Mode ALSA pur - pas de PulseAudio${N}"
  fi

  echo -e "${A}> Creation environnement virtuel Python...${N}"
  python3 -m venv "$SCRIPT_DIR/venv"
  source "$SCRIPT_DIR/venv/bin/activate"

  echo -e "${A}> Installation dependances Python...${N}"
  # ALSA_CARD force sounddevice/PortAudio sur la bonne carte
  pip install --quiet \
    flask \
    flask-socketio \
    "python-socketio[client]" \
    numpy scipy sounddevice matplotlib
  echo -e "${G}  OK Flask, SocketIO, numpy, scipy, sounddevice${N}"

  touch "$STAMP"
}

# ── Main ──
banner

case "${1:-}" in
  --install)
    install_server
    echo -e "${G}${B}  ✓ Installation terminée${N}"
    ;;

  --agent)
    source "$SCRIPT_DIR/venv/bin/activate" 2>/dev/null || true
    STATION_ID="${2:-1}"
    SERVER="${3:-http://localhost:5000}"
    echo -e "${C}> Lancement agent local — station $STATION_ID sur $SERVER${N}"
    echo ""
    # Lister les cartes ALSA disponibles
    echo -e "  Cartes ALSA disponibles :"
    cat /proc/asound/cards 2>/dev/null | sed "s/^/    /" || echo "    Aucune"
    echo ""
    # Variables d environnement ALSA pour forcer l acces direct
    export PA_ALSA_PLUGHW=1         # PortAudio utilise plughw (format flexible)
    export AUDIODEV=hw:0,0          # carte par defaut si besoin
    python3 "$SCRIPT_DIR/agent/agent.py" \
      --station-id "$STATION_ID" \
      --server "$SERVER" \
      --launch-rigctld \
      "${@:4}"
    ;;

  --devices)
    source "$SCRIPT_DIR/venv/bin/activate" 2>/dev/null || true
    python3 "$SCRIPT_DIR/agent/agent.py" --list-devices
    ;;

  *)
    # Lancement serveur (défaut)
    if [[ ! -f "$STAMP" ]]; then
      echo -e "${A}▸ Première exécution — installation...${N}"
      install_server
    else
      echo -e "${G}  ✓ Dépendances déjà installées${N}"
    fi

    source "$SCRIPT_DIR/venv/bin/activate"
    echo -e "${C}${B}▸ Lancement serveur NFM sur http://localhost:5000 ...${N}"
    echo ""
    echo -e "  ${A}Commandes utiles :${N}"
    echo -e "  ${C}./nfm.sh --agent 1${N}   ← lancer l'agent (station ID 1)"
    echo -e "  ${C}./nfm.sh --devices${N}   ← lister les cartes son"
    echo -e "  ${C}./nfm.sh --install${N}   ← forcer réinstallation"
    echo ""
    python3 "$SCRIPT_DIR/server.py"
    ;;
esac
