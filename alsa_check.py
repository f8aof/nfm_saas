#!/usr/bin/env python3
# =============================================================================
#  alsa_check.py — Diagnostic ALSA direct pour EMU 0202
#  Linux Mint 22.3 — Mode ALSA sans PulseAudio
#  Usage : python3 alsa_check.py
# =============================================================================

import os, sys, subprocess, platform

G = '\033[0;32m'
C = '\033[0;36m'
A = '\033[0;33m'
R = '\033[0;31m'
B = '\033[1m'
N = '\033[0m'

def run(cmd):
    try:
        return subprocess.check_output(cmd, shell=True,
               stderr=subprocess.STDOUT).decode().strip()
    except Exception:
        return ""

print(f"\n{C}{B}  DIAGNOSTIC ALSA — NFM Platform — F8AOF{N}")
print(f"  {'='*50}\n")

# ── 1. Système ──
print(f"{A}[1] Système{N}")
print(f"  OS      : {platform.system()} {platform.release()}")
print(f"  Python  : {sys.version.split()[0]}")
print()

# ── 2. Cartes ALSA (/proc/asound/cards) ──
print(f"{A}[2] Cartes son ALSA (/proc/asound/cards){N}")
cards_raw = run("cat /proc/asound/cards")
if cards_raw:
    for line in cards_raw.splitlines():
        line = line.strip()
        if not line:
            continue
        marker = f"{G}  ► {N}" if any(k in line.lower()
                  for k in ['emu', '0202', 'usb']) else "    "
        print(f"{marker}{line}")
else:
    print(f"  {R}Aucune carte détectée{N}")
print()

# ── 3. Périphériques ALSA enregistrement ──
print(f"{A}[3] Périphériques d'enregistrement (arecord -l){N}")
arecord = run("arecord -l")
if arecord:
    for line in arecord.splitlines():
        marker = f"{G}  ► {N}" if any(k in line.lower()
                  for k in ['emu', '0202', 'usb']) else "    "
        print(f"{marker}{line}")
else:
    print(f"  {A}arecord non disponible ou aucun périphérique{N}")
print()

# ── 4. PulseAudio / PipeWire ──
print(f"{A}[4] Serveur audio actif{N}")
pa_info = run("pactl info 2>/dev/null | head -5")
if pa_info:
    print(f"  {A}PulseAudio/PipeWire détecté :{N}")
    for line in pa_info.splitlines():
        print(f"    {line}")
    print(f"\n  {A}L'agent forcera ALSA direct via PA_ALSA_PLUGHW=1{N}")
else:
    print(f"  {G}Pas de PulseAudio/PipeWire — ALSA pur{N}")
print()

# ── 5. Groupe audio ──
print(f"{A}[5] Groupe audio{N}")
groups_out = run("groups")
if 'audio' in groups_out:
    print(f"  {G}OK — utilisateur dans le groupe audio{N}")
else:
    print(f"  {R}ATTENTION — pas dans le groupe audio{N}")
    print(f"  Solution : sudo usermod -aG audio $USER")
    print(f"  Puis déconnectez/reconnectez votre session")
print()

# ── 6. Test sounddevice ──
print(f"{A}[6] Test sounddevice (PortAudio){N}")
try:
    # Forcer ALSA avant import
    os.environ['PA_ALSA_PLUGHW'] = '1'
    import sounddevice as sd

    devs = sd.query_devices()
    inputs = [(i, d) for i, d in enumerate(devs)
              if d['max_input_channels'] > 0]

    print(f"  {len(inputs)} entrée(s) audio détectée(s) :\n")
    emu_found = False
    for i, d in inputs:
        name = d['name']
        sr   = int(d['default_samplerate'])
        ch   = d['max_input_channels']
        is_emu = any(k in name.lower() for k in ['emu', '0202'])
        is_virt= any(k in name.lower() for k in ['pulse', 'pipewire', 'default'])

        if is_emu:
            tag = f"{G}[EMU 0202] <-- UTILISER CELUI-CI{N}"
            emu_found = True
        elif is_virt:
            tag = f"{A}[VIRTUEL — eviter]{N}"
        else:
            tag = f"{C}[ALSA]{N}"

        print(f"    [{i:2d}] {name[:45]:<45} {ch}ch {sr}Hz  {tag}")

    print()
    if emu_found:
        print(f"  {G}OK — EMU 0202 trouvée{N}")
    else:
        print(f"  {A}EMU 0202 non trouvée dans sounddevice{N}")
        print(f"  Vérifiez la connexion USB et relancez")

except ImportError:
    print(f"  {R}sounddevice non installé{N}")
    print(f"  Lancez d'abord : ./nfm.sh --install")
except Exception as e:
    print(f"  {R}Erreur : {e}{N}")
print()

# ── 7. Test enregistrement court ──
print(f"{A}[7] Test enregistrement ALSA (1 seconde){N}")
try:
    import sounddevice as sd
    import numpy as np

    os.environ['PA_ALSA_PLUGHW'] = '1'

    # Trouver l'EMU
    devs = sd.query_devices()
    emu_idx = None
    for i, d in enumerate(devs):
        if d['max_input_channels'] > 0 and \
           any(k in d['name'].lower() for k in ['emu', '0202']):
            emu_idx = i
            break
    if emu_idx is None:
        for i, d in enumerate(devs):
            if d['max_input_channels'] > 0 and \
               not any(k in d['name'].lower() for k in ['pulse', 'pipewire', 'default']):
                emu_idx = i
                break

    if emu_idx is None:
        print(f"  {A}Aucun périphérique disponible pour le test{N}")
    else:
        dev_name = devs[emu_idx]['name']
        print(f"  Test sur : [{emu_idx}] {dev_name}")
        try:
            recording = sd.rec(
                frames=48000,
                samplerate=48000,
                channels=1,
                dtype='int32',
                device=emu_idx,
                blocking=True
            )
            audio = recording[:, 0].astype(np.float32) / 2147483648.0
            rms   = float(np.sqrt(np.mean(audio**2)))
            peak  = float(np.max(np.abs(audio)))
            rms_db  = 20 * np.log10(max(rms,  1e-10))
            peak_db = 20 * np.log10(max(peak, 1e-10))

            print(f"  {G}OK Enregistrement réussi{N}")
            print(f"  RMS  : {rms_db:.1f} dBFS")
            print(f"  Crête: {peak_db:.1f} dBFS")

            if rms_db < -90:
                print(f"  {A}Niveau très faible — vérifiez la connexion IC-706 → LINE IN{N}")
            elif rms_db > -6:
                print(f"  {R}Niveau trop élevé — risque d'écrêtage, réduire le volume AF{N}")
            else:
                print(f"  {G}Niveau correct pour la mesure NFM{N}")

        except Exception as e:
            print(f"  {R}Erreur enregistrement : {e}{N}")
            print(f"  {A}Essayez : python3 alsa_check.py avec PA_ALSA_PLUGHW=1{N}")

except Exception as e:
    print(f"  {A}Test ignoré : {e}{N}")

print()

# ── 8. Commande agent recommandée ──
print(f"{A}[8] Commande agent recommandée{N}")
print(f"\n  {C}# Lancer l'agent avec ALSA direct :{N}")
print(f"  export PA_ALSA_PLUGHW=1")
print(f"  python3 agent/agent.py --list-devices")
print(f"  python3 agent/agent.py --station-id 1 --server http://localhost:5000")
print()
print(f"  {C}# Si EMU 0202 sur card 1 (pas card 0) :{N}")
print(f"  export AUDIODEV=hw:1,0")
print(f"  python3 agent/agent.py --station-id 1 --device 2")
print()
print(f"  {C}# Depuis nfm.sh :{N}")
print(f"  ./nfm.sh --agent 1")
print()
print(f"  {'='*50}")
print(f"  {G}Diagnostic terminé{N}\n")
