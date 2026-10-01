# Doppler Media Companion

Controllo della musica con un gesto della mano, usando solo speaker e microfono come un **sonar a ultrasuoni**.

Lo speaker emette un tono continuo a ~19–20 kHz (quasi inudibile). La mano vicina al dispositivo riflette il suono: il software analizza lo spettro attorno alla frequenza emessa e rileva sia la **variazione di ampiezza** (presenza della mano) sia lo **shift Doppler** (la mano si avvicina o si allontana). Da questi segnali riconosce tre gesti e li trasforma in comandi multimediali.

| Gesto | Come si fa | Azione |
|---|---|---|
| **TAP SINGOLO** | avvicina e allontana subito la mano | play / pause |
| **DOPPIO TAP** | due contatti entro 0,4 s | traccia / video successivo |
| **HOLD** | tieni la mano vicina per 2 s | riavvia la traccia / il video |

## Contenuto della repo

```
doppler_hand.py        # sonar + riconoscimento gesti (script autonomo, stampa i gesti)
media_companion.py     # usa i gesti per controllare Spotify / YouTube su macOS
ios/DopplerSonarTest/  # porting iOS (SwiftUI) con controllo di Spotify
```

## Come funziona

La pipeline è la stessa in Python e in Swift:

1. **Emissione e acquisizione**: tono sinusoidale continuo in uscita, microfono in ascolto su un ring buffer.
2. **Analisi FFT** su due finestre:
   - una finestra piccola (1024 campioni, ~21 ms) per accorgersi velocemente dell'arrivo della mano;
   - una finestra grande (8192 campioni, con zero-padding) per stimare con precisione lo shift Doppler.
3. **Calibrazione**: all'avvio si misura la baseline con la mano lontana. Se il rilevatore resta "attivo" troppo a lungo, si ricalibra da solo.
4. **`PresenceDetector`** unisce ampiezza e Doppler in un unico stato attivo/inattivo stabile:
   - isteresi separata (soglia ON e OFF) su ciascun segnale;
   - unione in OR dei due segnali: lo shift Doppler copre i buchi di ampiezza dovuti al multipath;
   - lo shift Doppler viene ignorato quando l'energia riflessa è troppo bassa per fidarsi della stima;
   - debounce su N campioni consecutivi.
5. **`GestureDetector`**: una state machine a timer (`idle` → `present` → `waitSecond`) che dalla sequenza di stati attivo/inattivo riconosce TAP SINGOLO, DOPPIO TAP e HOLD.

## Script Python (macOS)

### Requisiti

- Python 3 con `numpy` e `sounddevice`

```bash
pip install numpy sounddevice
```

Speaker e microfono devono riuscire a riprodurre e captare frequenze vicine ai 20 kHz. I microfoni integrati dei Mac di solito ci riescono; se il segnale è debole, prova `--freq 19000`.

### `doppler_hand.py`: solo rilevamento

```bash
python doppler_hand.py
python doppler_hand.py --freq 19000 --samplerate 48000
```

Mostra in tempo reale ampiezza, shift Doppler e i gesti riconosciuti. Durante la calibrazione iniziale (1 s) tieni la mano lontana.

Parametri principali (`python doppler_hand.py --help` per l'elenco completo):

| Opzione | Default | Significato |
|---|---|---|
| `--freq` | 20000 | frequenza del tono (Hz) |
| `--samplerate` | 48000 | sample rate audio |
| `--amp-threshold` / `--amp-threshold-off` | 1.5 / 1.2 | soglie ON/OFF dell'ampiezza (multipli della baseline) |
| `--freq-shift-threshold` / `--freq-shift-threshold-off` | 14 / 8 | soglie ON/OFF dello shift Doppler (Hz); `0` disattiva la fusione Doppler |
| `--hold-time` | 2.0 | secondi di presenza continua per un HOLD |
| `--double-gap` | 0.4 | intervallo massimo tra due tap per contarli come doppio tap (s) |
| `--min-tap` | 0.05 | durata minima di un tap, sotto la quale è considerato rumore (s) |
| `--calib-time` | 1.0 | durata della calibrazione iniziale (s) |
| `--stuck-timeout` | 3.0 | secondi di stato attivo continuo dopo cui si ricalibra |

### `media_companion.py`: controllo multimediale

```bash
python media_companion.py
```

Accetta gli stessi parametri di `doppler_hand.py`, da cui importa `PresenceDetector`, `GestureDetector` e `parse_args`. Applica i gesti al contesto attivo, scelto in automatico:

- se Spotify e una tab YouTube in Chrome sono aperti insieme, comanda quello che sta suonando;
- se nessuno dei due sta suonando, comanda Spotify se è aperto, altrimenti YouTube.

Configurazione una tantum:

- **Spotify**: non serve nulla, viene controllato via AppleScript.
- **Chrome**: abilita *View → Developer → Allow JavaScript from Apple Events*.
- **Permessi macOS**: alla prima esecuzione concedi il controllo di Spotify, Google Chrome e System Events (*Impostazioni di Sistema → Privacy e sicurezza → Automazione*) e l'accesso al microfono.

## App iOS: `ios/DopplerSonarTest`

Porting SwiftUI della stessa pipeline (`SonarEngine`, `SpectrumAnalyzer`, `PresenceDetector`, `GestureDetector`) che controlla Spotify tramite lo **Spotify iOS SDK** (`SPTAppRemote`, v5.0.1 via Swift Package Manager).

L'app permette di:

- avviare e fermare il sonar, scegliere la frequenza e la modalità di `AVAudioSession` (Measurement / Default / VoiceChat);
- ricalibrare la baseline;
- vedere in tempo reale ampiezza, shift Doppler e lo stato di rilevamento;
- consultare il log dei gesti;
- collegarsi a Spotify e comandare la riproduzione con i gesti.

### Build

Requisiti: Xcode, iOS 16+, un iPhone fisico (il simulatore non ha un microfono adatto agli ultrasuoni).

Il progetto è generato con [XcodeGen](https://github.com/yonaskolb/XcodeGen) a partire da `project.yml`:

```bash
cd ios/DopplerSonarTest
xcodegen generate      # solo se modifichi project.yml
open DopplerSonarTest.xcodeproj
```

### Configurazione Spotify

1. Crea un'app su <https://developer.spotify.com/dashboard>.
2. Inserisci il Client ID in `Sources/SpotifyConfig.swift`.
3. Tra le *Redirect URIs* dell'app Spotify aggiungi `dopplersonar://spotify-callback`. Lo scheme è già registrato in `project.yml`; se lo cambi, aggiornalo in tutti e due i posti.
4. Sul telefono serve l'app Spotify installata e con una riproduzione già avviata.

## Limiti noti

- Il rilevamento dipende molto dall'hardware: non tutti gli speaker e microfoni gestiscono bene i 19–20 kHz.
- Superfici riflettenti e oggetti in movimento vicino al dispositivo possono generare falsi positivi. Se succede, ricalibra o alza le soglie.
- Alcune persone, e quasi tutti gli animali domestici, riescono a sentire il tono.
