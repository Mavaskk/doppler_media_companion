"""
Companion app per doppler_hand.py.

Ascolta il sonar a ultrasuoni (stessa logica di rilevamento gesti di
doppler_hand.py, importata da li' senza modificarlo) e traduce i gesti in
comandi multimediali, scegliendo automaticamente il contesto attivo tra
Spotify e una tab YouTube aperta in Google Chrome.

Perche' il loop audio e' riscritto qui: in doppler_hand.py l'acquisizione e
l'analisi FFT sono racchiuse dentro main() senza un punto di aggancio per un
callback sui gesti, quindi non e' richiamabile da fuori senza modificarlo. Le
parti davvero riusate sono quelle che contano: PresenceDetector,
GestureDetector e parse_args, importate direttamente da doppler_hand.

Mappatura gesti -> azione (sul contesto attivo, Spotify o YouTube):
  TAP SINGOLO -> play/pause
  DOPPIO TAP  -> traccia/video successivo
  HOLD        -> riavvia la traccia/video corrente

Contesto: se sia Spotify sia una tab YouTube sono aperti, ha priorita' chi
sta effettivamente suonando in quel momento; se nessuno dei due sta
suonando, priorita' a Spotify se e' aperto, altrimenti a YouTube.

Requisiti (macOS):
  - Spotify: nessuna configurazione, controllato via AppleScript.
  - Chrome: abilitare una tantum "Allow JavaScript from Apple Events" in
    Chrome -> View -> Developer.
  - Alla prima esecuzione macOS chiedera' il permesso di controllare
    Spotify / Google Chrome / System Events (Impostazioni di Sistema ->
    Privacy e sicurezza -> Automazione): va concesso.

Uso:
    python media_companion.py
    python media_companion.py --freq 19000 --samplerate 48000
"""

import subprocess
import sys
import time

import numpy as np
import sounddevice as sd

import doppler_hand as dh


# --------------------------------------------------------------- AppleScript

def _osascript(script):
    """Esegue uno script AppleScript e ritorna (output, ok)."""
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=3)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "", False
    return result.stdout.strip(), result.returncode == 0


# ------------------------------------------------------------------- Spotify

def spotify_is_running():
    out, ok = _osascript('tell application "System Events" to (name of processes) contains "Spotify"')
    return ok and out == "true"


def spotify_is_playing():
    out, ok = _osascript('tell application "Spotify" to player state as string')
    return ok and out == "playing"


def spotify_play_pause():
    _osascript('tell application "Spotify" to playpause')


def spotify_next():
    _osascript('tell application "Spotify" to next track')


def spotify_restart():
    _osascript('tell application "Spotify" to set player position to 0')


# ------------------------------------------------------------ Chrome/YouTube

_FIND_YT_TAB_SCRIPT = '''
tell application "Google Chrome"
    set winIdx to 0
    repeat with w in windows
        set winIdx to winIdx + 1
        set tabIdx to 0
        repeat with t in tabs of w
            set tabIdx to tabIdx + 1
            if (URL of t contains "youtube.com/watch") then
                return (winIdx as string) & "," & (tabIdx as string)
            end if
        end repeat
    end repeat
    return "0,0"
end tell
'''


def chrome_is_running():
    out, ok = _osascript('tell application "System Events" to (name of processes) contains "Google Chrome"')
    return ok and out == "true"


def find_youtube_tab():
    """Ritorna (finestra, tab) della prima tab YouTube trovata, o None."""
    if not chrome_is_running():
        return None
    out, ok = _osascript(_FIND_YT_TAB_SCRIPT)
    if not ok or out == "0,0":
        return None
    win, tab = out.split(",")
    return int(win), int(tab)


def _chrome_exec_js(win, tab, js):
    script = f'tell application "Google Chrome" to execute tab {tab} of window {win} javascript "{js}"'
    return _osascript(script)


def youtube_is_playing(win, tab):
    js = "(function(){var v=document.querySelector('video');return v && !v.paused;})()"
    out, ok = _chrome_exec_js(win, tab, js)
    return ok and out == "true"


def youtube_play_pause(win, tab):
    js = "(function(){var v=document.querySelector('video');if(v){v.paused?v.play():v.pause();}})()"
    _chrome_exec_js(win, tab, js)


def youtube_next(win, tab):
    js = (
        "(function(){"
        "var b=document.querySelector('.ytp-next-button');"
        "if(b){b.click();}"
        "else{document.dispatchEvent(new KeyboardEvent('keydown',{key:'N',shiftKey:true,bubbles:true}));}"
        "})()"
    )
    _chrome_exec_js(win, tab, js)


def youtube_restart(win, tab):
    js = "(function(){var v=document.querySelector('video');if(v){v.currentTime=0;}})()"
    _chrome_exec_js(win, tab, js)


# ----------------------------------------------------------------- Contesto

def choose_context():
    """Decide quale player controllare: preferisce chi sta effettivamente
    suonando in questo momento, altrimenti chi e' semplicemente aperto."""
    sp_running = spotify_is_running()
    if sp_running and spotify_is_playing():
        return "spotify", None

    yt = find_youtube_tab()
    if yt and youtube_is_playing(*yt):
        return "youtube", yt

    if sp_running:
        return "spotify", None
    if yt:
        return "youtube", yt

    return None, None


def handle_gesture(gesture):
    ctx, target = choose_context()
    if ctx is None:
        print(f"\n[{gesture}] nessun contesto attivo (Spotify/YouTube non trovati)")
        return

    if ctx == "spotify":
        action = {
            "TAP SINGOLO": spotify_play_pause,
            "DOPPIO TAP": spotify_next,
            "HOLD": spotify_restart,
        }.get(gesture)
    else:
        win, tab = target
        action = {
            "TAP SINGOLO": lambda: youtube_play_pause(win, tab),
            "DOPPIO TAP": lambda: youtube_next(win, tab),
            "HOLD": lambda: youtube_restart(win, tab),
        }.get(gesture)

    if action:
        action()
        print(f"\n[{ctx}] {gesture} -> eseguito")


# --------------------------------------------------------------- Sonar loop

def run():
    args = dh.parse_args()
    fs = args.samplerate
    f0 = args.freq

    if f0 > fs / 2 - 500:
        print(f"Attenzione: {f0} Hz e' troppo vicino a Nyquist ({fs/2} Hz) per questo samplerate.", file=sys.stderr)

    print(f"Emetto un tono a {f0:.0f} Hz e ascolto il microfono. Ctrl+C per fermare.\n")

    phase = np.array([0.0])

    def out_callback(outdata, frames, time_info, status):
        if status:
            print(status, file=sys.stderr)
        t = (np.arange(frames) + phase[0]) / fs
        outdata[:, 0] = 0.5 * np.sin(2 * np.pi * f0 * t)
        phase[0] += frames

    buf_len = args.fft_size
    ring = np.zeros(buf_len, dtype=np.float32)

    def in_callback(indata, frames, time_info, status):
        if status:
            print(status, file=sys.stderr)
        mono = indata[:, 0]
        n = len(mono)
        ring[:-n] = ring[n:]
        ring[-n:] = mono

    gesture_len = min(args.gesture_window, buf_len)
    window_small = np.hanning(gesture_len)
    freqs_small = np.fft.rfftfreq(gesture_len, d=1.0 / fs)
    band_mask_small = (freqs_small >= f0 - args.band) & (freqs_small <= f0 + args.band)

    # finestra grande per la stima Doppler (delta_f), stessa logica di doppler_hand.py
    window = np.hanning(buf_len)
    freqs = np.fft.rfftfreq(buf_len, d=1.0 / fs)
    band_mask = (freqs >= f0 - args.band) & (freqs <= f0 + args.band)
    band_freqs = freqs[band_mask]

    baseline_amp = None
    smoothed_amp = None

    presence = dh.PresenceDetector(threshold_on=args.amp_threshold, threshold_off=args.amp_threshold_off,
                                    debounce_samples=args.debounce_samples,
                                    freq_threshold_on=args.freq_shift_threshold or None,
                                    freq_threshold_off=args.freq_shift_threshold_off,
                                    freq_gate_ratio=args.freq_gate_ratio)
    detector = dh.GestureDetector(min_tap=args.min_tap, hold_time=args.hold_time, double_gap=args.double_gap)
    poll_ms = max(1, int(args.poll_interval * 1000))

    warmup_time = (buf_len / fs) * 1.2
    calib_samples = []
    calib_start = None
    active_since = None

    start_time = time.monotonic()

    try:
        with sd.OutputStream(samplerate=fs, blocksize=args.blocksize, channels=1,
                              callback=out_callback), \
             sd.InputStream(samplerate=fs, blocksize=args.blocksize, channels=1,
                             callback=in_callback):
            while True:
                sd.sleep(poll_ms)
                now = time.monotonic()

                if now - start_time < warmup_time:
                    continue  # il buffer non e' ancora pieno di audio reale

                small_slice = ring[-gesture_len:]
                spectrum_small = np.fft.rfft(small_slice * window_small)
                mag_small = np.abs(spectrum_small)
                band_mag_small = mag_small[band_mask_small]

                if band_mag_small.size == 0:
                    continue

                raw_peak_amp = band_mag_small.max()
                smoothed_amp = raw_peak_amp if smoothed_amp is None else 0.5 * smoothed_amp + 0.5 * raw_peak_amp
                peak_amp = smoothed_amp

                if baseline_amp is None:
                    if calib_start is None:
                        calib_start = now
                        print("Calibrazione in corso, tieni la mano lontana dal sensore...")
                    calib_samples.append(peak_amp)
                    if now - calib_start >= args.calib_time:
                        baseline_amp = float(np.median(calib_samples))
                        print(f"Calibrazione completata (baseline={baseline_amp:.4g}).\n")
                    continue

                # finestra grande: frequenza di picco per la stima Doppler (fusione in PresenceDetector)
                spectrum = np.fft.rfft(ring * window)
                mag = np.abs(spectrum)
                band_mag = mag[band_mask]
                peak_freq = band_freqs[np.argmax(band_mag)] if band_mag.size else f0
                delta_f = peak_freq - f0

                ratio = peak_amp / (baseline_amp + 1e-9)
                is_active = presence.update(ratio, delta_f)

                if not is_active:
                    baseline_amp = 0.98 * baseline_amp + 0.02 * peak_amp
                    active_since = None
                else:
                    if active_since is None:
                        active_since = now
                    elif now - active_since > args.stuck_timeout:
                        baseline_amp = peak_amp
                        active_since = None
                        presence = dh.PresenceDetector(threshold_on=args.amp_threshold,
                                                        threshold_off=args.amp_threshold_off,
                                                        debounce_samples=args.debounce_samples,
                                                        freq_threshold_on=args.freq_shift_threshold or None,
                                                        freq_threshold_off=args.freq_shift_threshold_off,
                                                        freq_gate_ratio=args.freq_gate_ratio)
                        detector = dh.GestureDetector(min_tap=args.min_tap, hold_time=args.hold_time,
                                                       double_gap=args.double_gap)
                        print("\n[ricalibrazione automatica: nessuna variazione per troppo tempo]")
                        continue

                gesture = detector.update(is_active)
                if gesture:
                    handle_gesture(gesture)

                bar_len = int(min(ratio, 4.0) * 15)
                bar = "#" * bar_len
                moving = "  <-- MOVIMENTO RILEVATO" if is_active else ""
                print(f"\rampiezza x{ratio:4.2f} {bar:<60}{moving}   ", end="", flush=True)

    except KeyboardInterrupt:
        print("\nFermato dall'utente.")
    except Exception as e:
        print(f"\nErrore: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    run()
