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
  HOLD        -> riavvia la traccia/video corrente; su YouTube, se c'e' una
                 pubblicita' con il pulsante "Salta" visibile, la salta

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
  - Per saltare le pubblicita' di YouTube serve anche il permesso di
    Accessibilita' per il terminale; opzionale `pip install
    pyobjc-framework-Quartz` per il fallback con click del mouse.

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


# ------------------------------------------------------- YouTube: salta ad
#
# YouTube ignora i click generati da JavaScript (element.click() ha
# isTrusted=false), quindi trovare il pulsante "Salta" non basta: il click
# deve arrivare dal sistema operativo. Si prova in ordine:
#   1. click() da JS (costa poco, nel caso YouTube lo accetti)
#   2. focus sul pulsante + tasto Invio vero inviato da System Events
#   3. click vero del mouse alle coordinate del pulsante (Quartz, se pyobjc
#      e' installato), riportando poi il cursore dov'era
# Il 2 e il 3 richiedono il permesso di Accessibilita' per il terminale
# (Impostazioni di Sistema -> Privacy e sicurezza -> Accessibilita').
# Le pubblicita' non saltabili non vengono toccate.

try:
    import Quartz
except ImportError:
    Quartz = None

# Classi del pulsante usate da YouTube nel tempo; se smette di funzionare,
# ispeziona il pulsante "Salta" in DevTools e aggiungi qui la nuova classe.
_SKIP_SELECTORS = [
    ".ytp-skip-ad-button",
    ".ytp-ad-skip-button-modern",
    ".ytp-ad-skip-button",
    ".videoAdUiSkipButton",
]

# f(): null se non c'e' pubblicita', false se c'e' ma senza pulsante "Salta"
# visibile, altrimenti il pulsante. Niente doppi apici: il JS finisce dentro
# una stringa AppleScript.
_FIND_SKIP_JS = (
    "function f(){"
    "var p=document.querySelector('#movie_player');"
    "if(!p||!p.classList.contains('ad-showing'))return null;"
    "var s=[" + ",".join(f"'{sel}'" for sel in _SKIP_SELECTORS) + "];"
    "for(var i=0;i<s.length;i++){var b=p.querySelector(s[i]);if(b&&b.offsetParent!==null)return b;}"
    "var bs=p.querySelectorAll('button');"
    "for(var j=0;j<bs.length;j++){"
    "var t=((bs[j].textContent||'')+' '+(bs[j].getAttribute('aria-label')||'')).toLowerCase();"
    "if((t.indexOf('skip')>=0||t.indexOf('salta')>=0)&&bs[j].offsetParent!==null)return bs[j];}"
    "return false;}"
)


def _skip_js(body):
    return "(function(){" + _FIND_SKIP_JS + "var b=f();" + body + "})()"


def youtube_ad_state(win, tab):
    """Ritorna 'no-ad', 'not-skippable' o 'skippable'."""
    out, ok = _chrome_exec_js(win, tab, _skip_js(
        "return b===null?'no-ad':(b===false?'not-skippable':'skippable');"))
    return out if ok else "no-ad"


def _ad_skipped(win, tab):
    time.sleep(0.4)
    return youtube_ad_state(win, tab) != "skippable"


def _frontmost_app():
    out, ok = _osascript('tell application "System Events" to get name of first application process whose frontmost is true')
    return out if ok else None


def _bring_tab_to_front(win, tab):
    """Porta in primo piano Chrome con la tab YouTube attiva. Ritorna lo
    stato precedente per ripristinarlo; dopo, la finestra e' la numero 1."""
    prev_app = _frontmost_app()
    prev_tab, _ = _osascript(f'tell application "Google Chrome" to get active tab index of window {win}')
    _osascript(f'''
tell application "Google Chrome"
    set active tab index of window {win} to {tab}
    set index of window {win} to 1
    activate
end tell
''')
    time.sleep(0.2)
    return prev_app, prev_tab


def _restore_front(prev_app, prev_tab, tab):
    if prev_tab and prev_tab != str(tab):
        _osascript(f'tell application "Google Chrome" to set active tab index of window 1 to {prev_tab}')
    if prev_app and prev_app != "Google Chrome":
        _osascript(f'tell application "{prev_app}" to activate')


def _skip_with_enter(tab):
    out, ok = _chrome_exec_js(1, tab, _skip_js(
        "if(!b)return 'gone';b.focus();return document.activeElement===b?'focused':'nofocus';"))
    if not ok or out != "focused":
        return False
    _, ok = _osascript('tell application "System Events" to key code 36')  # Invio
    if not ok:
        print("\n[youtube] tasto Invio non inviato: concedi l'Accessibilita' al terminale")
    return ok


def _skip_with_mouse(tab):
    if Quartz is None:
        return False
    # coordinate di schermo del centro del pulsante (assume zoom pagina 100%
    # e DevTools non agganciati in basso)
    out, ok = _chrome_exec_js(1, tab, _skip_js(
        "if(!b)return '';var r=b.getBoundingClientRect();"
        "return Math.round(window.screenX+r.left+r.width/2)+','+"
        "Math.round(window.screenY+(window.outerHeight-window.innerHeight)+r.top+r.height/2);"))
    if not ok or "," not in out:
        return False
    x, y = (float(v) for v in out.split(","))
    old_pos = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    for event_type in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        event = Quartz.CGEventCreateMouseEvent(None, event_type, (x, y), Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.05)
    Quartz.CGWarpMouseCursorPosition(old_pos)
    return True


def youtube_skip_ad(win, tab):
    """Prova a premere "Salta" con i metodi in ordine. Ritorna il metodo che
    ha funzionato, o None."""
    _chrome_exec_js(win, tab, _skip_js("if(b)b.click();"))
    if _ad_skipped(win, tab):
        return "click JS"

    prev_app, prev_tab = _bring_tab_to_front(win, tab)
    try:
        if _skip_with_enter(tab) and _ad_skipped(1, tab):
            return "tasto Invio"
        if _skip_with_mouse(tab) and _ad_skipped(1, tab):
            return "click mouse"
        return None
    finally:
        _restore_front(prev_app, prev_tab, tab)


def youtube_hold(win, tab):
    """HOLD su YouTube: salta la pubblicita' se si puo', altrimenti riavvia il video."""
    state = youtube_ad_state(win, tab)
    if state == "no-ad":
        youtube_restart(win, tab)
        return "video riavviato"
    if state == "not-skippable":
        return "pubblicita' non (ancora) saltabile, nessuna azione"
    method = youtube_skip_ad(win, tab)
    if method:
        return f"pubblicita' saltata ({method})"
    hint = "" if Quartz else "; installa pyobjc-framework-Quartz per il fallback col mouse"
    return f"pulsante 'Salta' trovato ma il click non e' andato a buon fine{hint}"


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
            "HOLD": lambda: youtube_hold(win, tab),
        }.get(gesture)

    if action:
        result = action()
        print(f"\n[{ctx}] {gesture} -> {result or 'eseguito'}")


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
