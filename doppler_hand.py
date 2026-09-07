"""
Sonar attivo a ultrasuoni per rilevare il movimento di una mano.

Principio:
  1. Lo speaker emette un tono continuo a ~20 kHz (quasi inudibile).
  2. La mano riflette/altera il suono che torna al microfono.
  3. Il microfono registra continuamente il segnale.
  4. Il software confronta lo spettro attorno a f0 nel tempo:
       - variazioni di ampiezza vicino a f0  -> presenza/riflesso della mano
       - spostamento della frequenza di picco -> effetto Doppler
         (mano che si avvicina = frequenza piu' alta, si allontana = piu' bassa)
  5. Ampiezza e shift Doppler vengono fusi in PresenceDetector (OR tra le due
     isteresi indipendenti, poi un debounce a campioni) in un booleano pulito
     attivo/inattivo: un vero movimento produce quasi sempre uno shift misurabile
     anche quando l'ampiezza ha un buco temporaneo da interferenza multipath,
     quindi la fusione rende il rilevamento piu' robusto ai drop-out di ampiezza
     durante un hold o tra i due contatti di un doppio tap.
  6. Da quella sequenza pulita una state machine esplicita a timer (GestureDetector)
     riconosce il gesto:
       - TAP SINGOLO: la mano si ritira subito e non torna
       - DOPPIO TAP: si ritira e torna entro --double-gap secondi
       - HOLD: rimane vicina per almeno --hold-time secondi

Uso:
    python doppler_hand.py
    python doppler_hand.py --freq 19000 --samplerate 48000
"""

import argparse
import sys
import time
from enum import Enum, auto

import numpy as np
import sounddevice as sd


def parse_args():
    p = argparse.ArgumentParser(description="Rilevatore di movimento a ultrasuoni (tipo sonar)")
    p.add_argument("--freq", type=float, default=20000.0, help="Frequenza del tono emesso in Hz (default 20000)")
    p.add_argument("--samplerate", type=int, default=48000, help="Sample rate audio (default 48000)")
    p.add_argument("--blocksize", type=int, default=2048, help="Campioni per blocco audio (default 2048)")
    p.add_argument("--band", type=float, default=400.0, help="Larghezza banda di ricerca attorno a f0 in Hz (default 400)")
    p.add_argument("--fft-size", type=int, default=8192, help="Dimensione FFT per l'analisi (default 8192, zero-padding per risoluzione)")
    p.add_argument("--amp-threshold", type=float, default=1.5, help="Soglia ON (in volte la baseline) per entrare in stato attivo (default 1.5)")
    p.add_argument("--amp-threshold-off", type=float, default=1.2, help="Soglia OFF per uscire da stato attivo, deve essere < amp-threshold: crea l'isteresi che evita lo sfarfallio vicino alla soglia (default 1.2)")
    p.add_argument("--debounce-samples", type=int, default=2, help="Campioni consecutivi concordi richiesti prima di cambiare stato attivo/inattivo (default 2)")
    p.add_argument("--freq-shift-threshold", type=float, default=14.0,
                   help="Soglia ON (Hz) sullo shift Doppler |delta_f| oltre la quale si considera "
                        "'movimento' anche se l'ampiezza non ha superato la soglia. Default 14.0 "
                        "(~2.4 bin FFT a fft-size/samplerate default, risoluzione 5.86Hz/bin). "
                        "Passare 0 per disabilitare la fusione Doppler (comportamento pre-fix).")
    p.add_argument("--freq-shift-threshold-off", type=float, default=8.0,
                   help="Soglia OFF (Hz) per l'isteresi sul lato Doppler, deve essere <= freq-shift-threshold "
                        "(default 8.0, ~1.4 bin).")
    p.add_argument("--freq-gate-ratio", type=float, default=1.1,
                   help="Ratio minimo di ampiezza sotto il quale lo shift Doppler viene ignorato perche' "
                        "inaffidabile (stimato su rumore, mano assente). Tra 1.0 (baseline) e amp-threshold-off "
                        "(default 1.1).")
    p.add_argument("--poll-interval", type=float, default=0.05, help="Intervallo tra un'analisi e l'altra in secondi (default 0.05)")
    p.add_argument("--min-tap", type=float, default=0.05, help="Durata minima (s) di un riflesso per contare come tap e non rumore (default 0.05)")
    p.add_argument("--hold-time", type=float, default=2.0, help="Durata (s) di presenza continua oltre la quale scatta 'HOLD' (default 2.0)")
    p.add_argument("--double-gap", type=float, default=0.4, help="Massimo intervallo (s) tra due tap per contarli come doppio tap (default 0.4)")
    p.add_argument("--calib-time", type=float, default=1.0, help="Durata (s) della calibrazione iniziale della baseline, mano lontana dal sensore (default 1.0)")
    p.add_argument("--stuck-timeout", type=float, default=3.0, help="Se resta 'attivo' oltre questo tempo (s) ricalibra automaticamente (default 3.0)")
    p.add_argument("--gesture-window", type=int, default=1024,
                   help="Campioni usati per la finestra 'veloce' con cui si decide attivo/inattivo (default 1024, ~21ms a 48kHz). "
                        "Deve essere <= --fft-size. Piu' piccola = piu' reattiva ai tap ravvicinati ma piu' rumorosa.")
    return p.parse_args()


class PresenceDetector:
    """Fonde ampiezza e shift Doppler in un booleano attivo/inattivo stabile.

    Tre meccanismi indipendenti, tutti necessari:
      - isteresi su ciascun segnale: soglia piu' alta per ENTRARE in attivo,
        piu' bassa per USCIRNE. Senza isteresi, un segnale che oscilla intorno
        a un'unica soglia genera raffiche di transizioni spurie.
      - fusione OR tra ampiezza e Doppler: un vero movimento produce quasi
        sempre uno shift di frequenza misurabile anche quando l'ampiezza ha
        un buco temporaneo da interferenza multipath (onda diretta + riflessa
        che si cancellano nello spazio) - il segnale che "vede" il movimento
        in quel momento tiene attivo lo stato finche' l'altro non recupera.
      - gate di attendibilita' sul Doppler: quando non c'e' energia riflessa
        (mano lontana), lo spettro in banda e' dominato da rumore e il picco
        di frequenza puo' saltare a caso -> lo shift stimato in quei momenti
        non e' affidabile e va ignorato, altrimenti il rumore da solo
        genererebbe falsi positivi.
      - debounce a campioni sul risultato combinato: richiede N letture
        consecutive concordi prima di confermare il cambio di stato finale,
        cosi' un singolo campione rumoroso non genera un blip che poi
        confonde i timer del gesto.
    """

    def __init__(self, threshold_on, threshold_off, debounce_samples=2,
                 freq_threshold_on=None, freq_threshold_off=None, freq_gate_ratio=1.1):
        if threshold_off > threshold_on:
            raise ValueError("threshold_off deve essere <= threshold_on (isteresi)")
        if freq_threshold_on is not None:
            if freq_threshold_off is None or freq_threshold_off > freq_threshold_on:
                raise ValueError("freq_threshold_off deve essere <= freq_threshold_on (isteresi)")

        self.threshold_on = threshold_on
        self.threshold_off = threshold_off
        self.freq_threshold_on = freq_threshold_on    # None = fusione Doppler disabilitata
        self.freq_threshold_off = freq_threshold_off
        self.freq_gate_ratio = freq_gate_ratio
        self.debounce_samples = max(1, debounce_samples)

        self.is_active = False
        self._amp_active = False
        self._freq_active = False
        self._pending_count = 0

    def update(self, amp_ratio, freq_shift_hz=0.0):
        amp_threshold = self.threshold_off if self._amp_active else self.threshold_on
        self._amp_active = amp_ratio > amp_threshold

        if self.freq_threshold_on is not None and amp_ratio >= self.freq_gate_ratio:
            freq_threshold = self.freq_threshold_off if self._freq_active else self.freq_threshold_on
            self._freq_active = abs(freq_shift_hz) > freq_threshold
        else:
            self._freq_active = False  # nessuna energia riflessa affidabile -> non fidarsi dello shift

        candidate = self._amp_active or self._freq_active

        if candidate != self.is_active:
            self._pending_count += 1
            if self._pending_count >= self.debounce_samples:
                self.is_active = candidate
                self._pending_count = 0
        else:
            self._pending_count = 0

        return self.is_active


class GestureState(Enum):
    IDLE = auto()         # mano lontana, nessun contatto in corso
    PRESENT = auto()      # mano vicina (primo o secondo contatto)
    WAIT_SECOND = auto()  # mano appena ritirata dopo il primo contatto: in attesa di un eventuale secondo


class GestureDetector:
    """State machine esplicita a timer su una sequenza pulita di presenza/assenza.

    Criteri (vedi docstring del modulo):
      - si ritira subito e non torna         -> TAP SINGOLO
      - si ritira e torna entro double_gap   -> DOPPIO TAP
      - resta presente per >= hold_time      -> HOLD
    """

    def __init__(self, min_tap, hold_time, double_gap):
        self.min_tap = min_tap
        self.hold_time = hold_time
        self.double_gap = double_gap

        self.state = GestureState.IDLE
        self.present_start = None
        self.tap_index = 0        # 1 = primo contatto, 2 = secondo (candidato doppio tap)
        self.hold_reported = False
        self.release_time = None  # istante in cui e' finito il primo contatto

    def update(self, is_active, now=None):
        """Chiamata a ogni ciclo di analisi. Ritorna una stringa con il gesto rilevato, o None."""
        now = time.monotonic() if now is None else now
        gesture = None

        if self.state == GestureState.IDLE:
            if is_active:
                self._enter_present(tap_index=1, now=now)

        elif self.state == GestureState.WAIT_SECOND:
            if is_active:
                self._enter_present(tap_index=2, now=now)
            elif now - self.release_time > self.double_gap:
                # nessun secondo contatto arrivato in tempo -> era un tap singolo
                gesture = "TAP SINGOLO"
                self.state = GestureState.IDLE

        elif self.state == GestureState.PRESENT:
            duration = now - self.present_start
            if is_active:
                if duration >= self.hold_time and not self.hold_reported:
                    gesture = "HOLD"
                    self.hold_reported = True
            else:
                if self.hold_reported:
                    self.state = GestureState.IDLE  # gia' segnalato durante la presenza
                elif duration < self.min_tap:
                    self.state = GestureState.IDLE  # troppo breve, probabile rumore residuo
                elif self.tap_index == 1:
                    self.state = GestureState.WAIT_SECOND
                    self.release_time = now
                else:  # tap_index == 2
                    gesture = "DOPPIO TAP"
                    self.state = GestureState.IDLE

        return gesture

    def _enter_present(self, tap_index, now):
        self.state = GestureState.PRESENT
        self.present_start = now
        self.tap_index = tap_index
        self.hold_reported = False


def main():
    args = parse_args()
    fs = args.samplerate
    f0 = args.freq

    if f0 > fs / 2 - 500:
        print(f"Attenzione: {f0} Hz e' troppo vicino a Nyquist ({fs/2} Hz) per questo samplerate.", file=sys.stderr)

    print(sd.query_devices())
    print(f"\nEmetto un tono a {f0:.0f} Hz e ascolto il microfono. Ctrl+C per fermare.\n")

    # --- generatore del tono in uscita (callback, phase continua) ---
    phase = np.array([0.0])

    def out_callback(outdata, frames, time_info, status):
        if status:
            print(status, file=sys.stderr)
        t = (np.arange(frames) + phase[0]) / fs
        outdata[:, 0] = 0.5 * np.sin(2 * np.pi * f0 * t)
        phase[0] += frames

    # --- buffer circolare per l'ingresso microfono ---
    buf_len = args.fft_size
    ring = np.zeros(buf_len, dtype=np.float32)

    def in_callback(indata, frames, time_info, status):
        if status:
            print(status, file=sys.stderr)
        mono = indata[:, 0]
        n = len(mono)
        ring[:-n] = ring[n:]
        ring[-n:] = mono

    # finestra "grande": usata per la stima precisa della frequenza (Doppler)
    window = np.hanning(buf_len)
    freqs = np.fft.rfftfreq(buf_len, d=1.0 / fs)
    band_mask = (freqs >= f0 - args.band) & (freqs <= f0 + args.band)
    band_freqs = freqs[band_mask]

    # finestra "piccola": presa dalla coda dello stesso buffer, usata per decidere
    # attivo/inattivo con bassa latenza (necessaria per distinguere i colpi di un doppio tap,
    # che con la finestra grande verrebbero "fusi" in un unico evento continuo)
    gesture_len = min(args.gesture_window, buf_len)
    window_small = np.hanning(gesture_len)
    freqs_small = np.fft.rfftfreq(gesture_len, d=1.0 / fs)
    band_mask_small = (freqs_small >= f0 - args.band) & (freqs_small <= f0 + args.band)

    baseline_amp = None
    smoothed_amp = None  # leggero filtro anti-jitter sulla finestra piccola
    speed_of_sound = 343.0  # m/s in aria

    presence = PresenceDetector(threshold_on=args.amp_threshold, threshold_off=args.amp_threshold_off,
                                 debounce_samples=args.debounce_samples,
                                 freq_threshold_on=args.freq_shift_threshold or None,
                                 freq_threshold_off=args.freq_shift_threshold_off,
                                 freq_gate_ratio=args.freq_gate_ratio)
    detector = GestureDetector(min_tap=args.min_tap, hold_time=args.hold_time, double_gap=args.double_gap)
    poll_ms = max(1, int(args.poll_interval * 1000))

    # tempo perche' il ring buffer si riempia completamente di audio reale
    warmup_time = (buf_len / fs) * 1.2
    calib_samples = []
    calib_start = None
    active_since = None  # per il timeout di sicurezza anti-blocco

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

                # --- finestra piccola: ampiezza in banda per la soglia attivo/inattivo ---
                small_slice = ring[-gesture_len:]
                spectrum_small = np.fft.rfft(small_slice * window_small)
                mag_small = np.abs(spectrum_small)
                band_mag_small = mag_small[band_mask_small]

                if band_mag_small.size == 0:
                    continue

                raw_peak_amp = band_mag_small.max()
                # leggerissimo filtro per non far scattare la soglia su un singolo campione rumoroso
                smoothed_amp = raw_peak_amp if smoothed_amp is None else 0.5 * smoothed_amp + 0.5 * raw_peak_amp
                peak_amp = smoothed_amp

                if baseline_amp is None:
                    # fase di calibrazione: media di piu' campioni, non uno solo
                    if calib_start is None:
                        calib_start = now
                        print("Calibrazione in corso, tieni la mano lontana dal sensore...")
                    calib_samples.append(peak_amp)
                    if now - calib_start >= args.calib_time:
                        baseline_amp = float(np.median(calib_samples))
                        print(f"Calibrazione completata (baseline={baseline_amp:.4g}).\n")
                    continue

                # --- finestra grande: frequenza di picco per la stima Doppler ---
                # anticipata rispetto alla decisione is_active, cosi' delta_f e'
                # disponibile per la fusione in PresenceDetector (non piu' solo display)
                spectrum = np.fft.rfft(ring * window)
                mag = np.abs(spectrum)
                band_mag = mag[band_mask]
                peak_freq = band_freqs[np.argmax(band_mag)] if band_mag.size else f0

                delta_f = peak_freq - f0
                # stima velocita' radiale (formula radar: riflesso "andata e ritorno")
                velocity = delta_f * speed_of_sound / (2 * f0)

                ratio = peak_amp / (baseline_amp + 1e-9)
                is_active = presence.update(ratio, delta_f)

                # la baseline si aggiorna solo quando la mano NON e' presente, altrimenti
                # durante un tap prolungato "insegue" il segnale e perde il rilevamento
                if not is_active:
                    baseline_amp = 0.98 * baseline_amp + 0.02 * peak_amp
                    active_since = None
                else:
                    if active_since is None:
                        active_since = now
                    elif now - active_since > args.stuck_timeout:
                        # bloccato "attivo" troppo a lungo: probabilmente la baseline
                        # e' partita male (es. rumore all'avvio) -> ricalibra sul volo
                        baseline_amp = peak_amp
                        active_since = None
                        presence = PresenceDetector(threshold_on=args.amp_threshold,
                                                     threshold_off=args.amp_threshold_off,
                                                     debounce_samples=args.debounce_samples,
                                                     freq_threshold_on=args.freq_shift_threshold or None,
                                                     freq_threshold_off=args.freq_shift_threshold_off,
                                                     freq_gate_ratio=args.freq_gate_ratio)
                        detector = GestureDetector(min_tap=args.min_tap, hold_time=args.hold_time,
                                                    double_gap=args.double_gap)
                        print("\n[ricalibrazione automatica: nessuna variazione per troppo tempo]")
                        continue

                bar_len = int(min(ratio, 4.0) * 15)
                bar = "#" * bar_len

                moving = "  <-- MOVIMENTO RILEVATO" if is_active else ""
                direction = ""
                if abs(delta_f) > 5:
                    direction = "avvicina" if delta_f > 0 else "allontana"

                gesture = detector.update(is_active)
                if gesture:
                    print(f"\n>>> {gesture}")

                print(f"\rΔf={delta_f:+7.1f}Hz  v≈{velocity:+6.2f}m/s  ampiezza x{ratio:4.2f} {bar:<60}{moving} {direction}   ",
                      end="", flush=True)

    except KeyboardInterrupt:
        print("\nFermato dall'utente.")
    except Exception as e:
        print(f"\nErrore: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
