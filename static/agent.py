#!/usr/bin/env python3
# =============================================================================
#  NFM SaaS — Agent local
#  Tourne sur le PC de l'utilisateur (Linux ou Windows)
#  Audio ALSA/Windows → FFT/PSD → WebSocket → Serveur NFM
#  Contrôle CAT Hamlib via rigctld
#
#  Usage : python3 agent.py --station-id 1 --server ws://localhost:5000
# =============================================================================

import sys, os, time, json, argparse, threading, socket, queue, subprocess
from collections import deque
import numpy as np
from scipy import signal as scipy_signal
from scipy.stats import scoreatpercentile

try:
    import sounddevice as sd
    AUDIO_OK = True
except ImportError:
    print("  ⚠ sounddevice non installé : pip install sounddevice")
    AUDIO_OK = False

try:
    import socketio as sio_client
    SIO_OK = True
except ImportError:
    print("  ⚠ python-socketio non installé : pip install 'python-socketio[client]'")
    SIO_OK = False

# =============================================================================
#  CONFIG
# =============================================================================
SAMPLE_RATE = 48000
BLOCK_SIZE  = 4096
FFT_DEFAULT = 4096
MAX_AVG     = 64
SEND_INTERVAL = 0.5   # secondes entre envois WebSocket
PSD_INTERVAL  = 2.0   # secondes entre envois PSD complet

# =============================================================================
#  ÉTAT
# =============================================================================
class AgentState:
    def __init__(self):
        self.running      = False
        self.fft_size     = FFT_DEFAULT
        self.window_name  = "hann"
        self.n_avg        = 32
        self.percentile   = 10
        self.bits         = 24
        self.device_idx   = None
        self.device_name  = ""

        self.audio_queue  = queue.Queue(maxsize=128)
        self.psd_frames   = deque(maxlen=MAX_AVG)
        self.psd_avg      = None
        self.freqs        = None

        self.nf_current   = -999.0
        self.rms_dbfs     = -999.0
        self.peak_dbfs    = -999.0
        self.frame_count  = 0
        self.clip_count   = 0

        # CAT
        self.cat_freq_hz  = None
        self.cat_mode     = None
        self.cat_sock     = None
        self.cat_lock     = threading.Lock()

        # WebSocket
        self.sio          = None
        self.station_id   = None
        self.server_url   = ""
        self.connected    = False

        self._win         = None
        self._win_key     = None
        self.lock         = threading.Lock()

    def get_window(self):
        key = (self.window_name, self.fft_size)
        if self._win_key != key:
            N = self.fft_size
            name = self.window_name
            if name == "hann":       w = np.hanning(N)
            elif name == "blackman": w = np.blackman(N)
            elif name == "flattop":  w = scipy_signal.windows.flattop(N)
            else:                    w = np.ones(N)
            w = w / np.sqrt(np.mean(w**2))
            self._win = w.astype(np.float32)
            self._win_key = key
        return self._win

ST = AgentState()

# =============================================================================
#  LISTAGE DES PÉRIPHÉRIQUES (Linux + Windows)
# =============================================================================
def list_audio_devices():
    """Liste les entrées audio disponibles sur le système."""
    if not AUDIO_OK:
        return []
    devices = []
    try:
        devs = sd.query_devices()
        for i, d in enumerate(devs):
            if d["max_input_channels"] > 0:
                devices.append({
                    "index":       i,
                    "name":        d["name"],
                    "channels":    d["max_input_channels"],
                    "sample_rate": int(d["default_samplerate"]),
                    "preferred":   any(k in d["name"].lower()
                                      for k in ["emu","0202","usb","line","codec"]),
                })
    except Exception as e:
        print(f"  Erreur liste devices : {e}")
    return devices

def find_device(name_hint=None):
    """Trouve le device ALSA/Windows correspondant au nom enregistré."""
    devs = list_audio_devices()
    if not devs:
        return None
    if name_hint:
        for d in devs:
            if name_hint.lower() in d["name"].lower():
                return d["index"]
    # Préférer EMU 0202 ou USB Audio
    preferred = [d for d in devs if d["preferred"]]
    return preferred[0]["index"] if preferred else devs[0]["index"]

def print_devices():
    print("\n  PÉRIPHÉRIQUES AUDIO DISPONIBLES")
    print("  " + "─"*50)
    for d in list_audio_devices():
        marker = "►" if d["preferred"] else " "
        print(f"  [{d['index']:2d}] {marker} {d['name'][:45]}"
              f"  ({d['channels']}ch · {d['sample_rate']} Hz)")
    print("  " + "─"*50 + "\n")

# =============================================================================
#  DSP
# =============================================================================
def compute_psd(block, window, fft_size, sr):
    x = block[:fft_size] * window
    spec = np.fft.rfft(x, n=fft_size)
    pow_lin = (np.abs(spec)**2) / fft_size
    pow_lin[1:-1] *= 2
    psd_lin = pow_lin / (sr / fft_size)
    return 10.0 * np.log10(np.maximum(psd_lin, 1e-30))

def audio_callback(indata, frames, time_info, status):
    if not ST.running: return
    mono = indata[:, 0].copy().astype(np.float32)
    try:
        ST.audio_queue.put_nowait(mono)
    except queue.Full:
        pass

def dsp_thread():
    block_buf = np.zeros(0, dtype=np.float32)
    last_send  = 0
    last_psd   = 0

    while True:
        if not ST.running:
            time.sleep(0.05)
            continue
        try:
            chunk = ST.audio_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        block_buf = np.concatenate([block_buf, chunk])
        if len(block_buf) < ST.fft_size:
            continue

        block = block_buf[:ST.fft_size]
        block_buf = block_buf[ST.fft_size // 2:]

        window  = ST.get_window()
        psd_db  = compute_psd(block, window, ST.fft_size, SAMPLE_RATE)

        peak  = float(np.max(np.abs(block)))
        rms   = float(np.sqrt(np.mean(block**2)))
        rms_db  = 20.0 * np.log10(max(rms,  1e-15))
        peak_db = 20.0 * np.log10(max(peak, 1e-15))

        if peak >= 0.99:
            ST.clip_count += 1

        psd_lin = 10.0 ** (psd_db / 10.0)
        ST.psd_frames.append(psd_lin)
        n_use = min(ST.n_avg, len(ST.psd_frames))
        avg_lin = np.mean(list(ST.psd_frames)[-n_use:], axis=0)
        psd_avg = 10.0 * np.log10(np.maximum(avg_lin, 1e-30))

        nf    = float(scoreatpercentile(psd_avg, ST.percentile))
        freqs = np.fft.rfftfreq(ST.fft_size, 1.0 / SAMPLE_RATE)

        with ST.lock:
            ST.psd_avg    = psd_avg
            ST.freqs      = freqs
            ST.nf_current = nf
            ST.rms_dbfs   = rms_db
            ST.peak_dbfs  = peak_db
            ST.frame_count += 1

        now = time.time()

        # Envoi mesure condensée
        if ST.connected and now - last_send >= SEND_INTERVAL:
            try:
                ST.sio.emit("measurement", {
                    "station_id": ST.station_id,
                    "timestamp":  now,
                    "nf_dbfs":    round(nf, 3),
                    "rms_dbfs":   round(rms_db, 3),
                    "peak_dbfs":  round(peak_db, 3),
                    "freq_hz":    ST.cat_freq_hz,
                    "mode":       ST.cat_mode,
                    "clip_count": ST.clip_count,
                })
                last_send = now
            except Exception:
                pass

        # Envoi PSD complet (moins fréquent)
        if ST.connected and now - last_psd >= PSD_INTERVAL:
            try:
                ST.sio.emit("psd_data", {
                    "station_id":  ST.station_id,
                    "timestamp":   now,
                    "fft_size":    ST.fft_size,
                    "sample_rate": SAMPLE_RATE,
                    "freqs":       freqs.tolist()[::4],     # décimé ×4 pour le réseau
                    "psd":         psd_avg.tolist()[::4],
                    "nf_dbfs":     round(nf, 3),
                })
                last_psd = now
            except Exception:
                pass

# =============================================================================
#  CAT HAMLIB
# =============================================================================
def cat_connect(host="127.0.0.1", port=4532):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3.0)
        s.connect((host, port))
        ST.cat_sock = s
        print(f"  ✓ CAT rigctld connecté sur {host}:{port}")
        t = threading.Thread(target=cat_poll_loop, daemon=True)
        t.start()
        return True
    except Exception as e:
        print(f"  ⚠ CAT non disponible : {e}")
        return False

def cat_send(cmd):
    try:
        with ST.cat_lock:
            ST.cat_sock.sendall((cmd + "\n").encode())
            resp = b""
            while not resp.endswith(b"\n"):
                chunk = ST.cat_sock.recv(256)
                if not chunk: break
                resp += chunk
            return resp.decode().strip()
    except Exception:
        return None

def cat_poll_loop():
    while ST.cat_sock:
        try:
            freq = cat_send("f")
            if freq and freq.isdigit():
                ST.cat_freq_hz = int(freq)
            mode = cat_send("m")
            if mode:
                ST.cat_mode = mode.split()[0]
            time.sleep(0.5)
        except Exception:
            break

def launch_rigctld(hamlib_id, port, baud, ptt="RTS"):
    """Lance rigctld en sous-processus."""
    cmd = ["rigctld", "-m", str(hamlib_id),
           "-r", port, "-s", str(baud), "-P", ptt]
    print(f"  → Lancement rigctld : {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(cmd,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        return proc
    except FileNotFoundError:
        print("  ⚠ rigctld introuvable — Hamlib non installé ?")
        return None

# =============================================================================
#  WEBSOCKET CLIENT
# =============================================================================
def connect_to_server(server_url, station_id):
    if not SIO_OK:
        print("  ERREUR : python-socketio non installé")
        return False
    try:
        ST.sio = sio_client.Client(reconnection=True, reconnection_delay=2)

        @ST.sio.event
        def connect():
            ST.connected = True
            print(f"  ✓ WebSocket connecté → {server_url}")
            ST.sio.emit("agent_connect", {
                "station_id": station_id,
                "token":      "",
            })

        @ST.sio.event
        def disconnect():
            ST.connected = False
            print("  ⚠ WebSocket déconnecté — reconnexion auto…")

        @ST.sio.event
        def agent_ok(data):
            print(f"  ✓ Station enregistrée : {data.get('name')}")

        @ST.sio.on("cat_command")
        def on_cat_command(data):
            """Commande CAT reçue du navigateur."""
            cmd_type = data.get("type")
            if cmd_type == "set_freq" and ST.cat_sock:
                hz = int(data.get("freq_hz", 0))
                cat_send(f"F {hz}")
            elif cmd_type == "set_mode" and ST.cat_sock:
                mode = data.get("mode","USB")
                cat_send(f"M {mode} 0")

        @ST.sio.on("config_update")
        def on_config_update(data):
            """Mise à jour de la config depuis le serveur."""
            if "fft_size" in data:    ST.fft_size    = int(data["fft_size"])
            if "fft_window" in data:  ST.window_name = data["fft_window"]
            if "fft_avg" in data:     ST.n_avg       = int(data["fft_avg"])
            if "percentile" in data:  ST.percentile  = int(data["percentile"])
            print(f"  Config mise à jour : {data}")

        @ST.sio.on("error")
        def on_error(data):
            print(f"  ✗ Erreur serveur : {data.get('msg')}")

        ST.sio.connect(server_url, transports=["websocket"])
        return True
    except Exception as e:
        print(f"  ERREUR WebSocket : {e}")
        return False

# =============================================================================
#  MAIN
# =============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="NFM SaaS Agent — F8AOF",
        formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--station-id", "-s", type=int, required=False,
                        help="ID de la station (voir dashboard)")
    parser.add_argument("--server", default="http://localhost:5000",
                        help="URL du serveur NFM (défaut: http://localhost:5000)")
    parser.add_argument("--device", "-d", type=int, default=None,
                        help="Index du périphérique audio")
    parser.add_argument("--device-name", default=None,
                        help="Nom partiel de la carte son (ex: EMU, USB)")
    parser.add_argument("--bits", type=int, choices=[16,24,32], default=24)
    parser.add_argument("--fft", type=int, default=4096)
    parser.add_argument("--cat-host", default="127.0.0.1")
    parser.add_argument("--cat-port", type=int, default=4532)
    parser.add_argument("--rig-id", type=int, default=None,
                        help="ID Hamlib du transceiver (ex: 3021 pour IC-706MkIIG)")
    parser.add_argument("--rig-port", default=None,
                        help="Port série (ex: /dev/ttyUSB0 ou COM3)")
    parser.add_argument("--rig-baud", type=int, default=9600)
    parser.add_argument("--launch-rigctld", action="store_true",
                        help="Lancer rigctld automatiquement")
    parser.add_argument("--list-devices", "-l", action="store_true",
                        help="Lister les périphériques audio et quitter")
    args = parser.parse_args()

    print("\n  ╔══════════════════════════════════════════════╗")
    print("  ║   NFM SaaS Agent — F8AOF                    ║")
    print("  ╚══════════════════════════════════════════════╝\n")

    if args.list_devices:
        print_devices()
        sys.exit(0)

    if not args.station_id:
        print("  ERREUR : --station-id requis (voir votre dashboard NFM)\n")
        parser.print_help()
        sys.exit(1)

    print_devices()

    # ── Sélection de la carte son ──
    if args.device is not None:
        ST.device_idx = args.device
    else:
        ST.device_idx = find_device(args.device_name)
        if ST.device_idx is None:
            print("  ERREUR : aucun périphérique audio disponible")
            sys.exit(1)

    devs = sd.query_devices()
    ST.device_name = devs[ST.device_idx]["name"]
    ST.fft_size    = args.fft
    ST.bits        = args.bits
    ST.station_id  = args.station_id

    print(f"  Périphérique sélectionné : [{ST.device_idx}] {ST.device_name}")
    print(f"  Résolution : {args.bits} bits / {SAMPLE_RATE} Hz")
    print(f"  FFT : {args.fft} pts → {SAMPLE_RATE/args.fft:.1f} Hz/bin")
    print(f"  Station ID : {args.station_id}")
    print(f"  Serveur : {args.server}\n")

    # ── rigctld ──
    rigctld_proc = None
    if args.rig_id and args.rig_port:
        if args.launch_rigctld:
            rigctld_proc = launch_rigctld(
                args.rig_id, args.rig_port, args.rig_baud)
        if cat_connect(args.cat_host, args.cat_port):
            print(f"  CAT actif — rig Hamlib {args.rig_id} sur {args.rig_port}")
    else:
        print("  ℹ CAT désactivé (--rig-id et --rig-port non fournis)")

    # ── Thread DSP ──
    t_dsp = threading.Thread(target=dsp_thread, daemon=True)
    t_dsp.start()

    # ── Connexion WebSocket ──
    print(f"  Connexion au serveur NFM…")
    if not connect_to_server(args.server, args.station_id):
        print("  ERREUR : impossible de se connecter. Le serveur est-il lancé ?")
        sys.exit(1)

    # ── Stream audio ──
    print(f"  Démarrage de la capture audio…\n")
    try:
        ST.running = True
        with sd.InputStream(
            device=ST.device_idx,
            channels=1,
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE,
            dtype="float32",
            callback=audio_callback,
            latency="low",
        ):
            print("  ✓ Agent en service — Ctrl+C pour arrêter\n")
            while True:
                time.sleep(5)
                if ST.frame_count > 0:
                    print(f"  [{time.strftime('%H:%M:%S')}] "
                          f"NF={ST.nf_current:.1f} dBFS/Hz  "
                          f"RMS={ST.rms_dbfs:.1f} dBFS  "
                          f"Trames={ST.frame_count}  "
                          f"{'CAT: '+str(ST.cat_freq_hz//1000)+'kHz' if ST.cat_freq_hz else 'CAT: ---'}")

    except KeyboardInterrupt:
        print("\n  Arrêt de l'agent.")
        ST.running = False
        if ST.sio: ST.sio.disconnect()
        if rigctld_proc: rigctld_proc.terminate()

if __name__ == "__main__":
    main()
