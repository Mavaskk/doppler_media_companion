import Foundation
import AVFoundation

/// Porta di doppler_hand.py: emette un tono continuo, ascolta il microfono e
/// riconosce TAP SINGOLO / DOPPIO TAP / HOLD dalla mano che riflette il
/// suono. Stessa architettura del Python: ring buffer, finestra piccola per
/// l'onset veloce, finestra grande per lo shift Doppler, fusione in
/// PresenceDetector, state machine in GestureDetector.
final class SonarEngine: ObservableObject {

    enum SessionMode: String, CaseIterable, Identifiable {
        case measurement = "Measurement"
        case defaultMode = "Default"
        case voiceChat = "VoiceChat"

        var id: String { rawValue }

        var avMode: AVAudioSession.Mode {
            switch self {
            case .measurement: return .measurement
            case .defaultMode: return .default
            case .voiceChat: return .voiceChat
            }
        }
    }

    struct LogEntry: Identifiable {
        let id = UUID()
        let time: Date
        let text: String
    }

    // Parametri configurabili dall'utente (letti solo a start(), come parse_args() in Python)
    @Published var sessionMode: SessionMode = .measurement
    @Published var frequency: Double = 19000

    // Stato pubblicato per la UI
    @Published private(set) var isRunning = false
    @Published private(set) var statusText = "Fermo"
    @Published private(set) var errorText: String?

    @Published private(set) var peakRatio: Double = 0
    @Published private(set) var deltaF: Double = 0
    @Published private(set) var peakAmp: Double = 0
    @Published private(set) var baseline: Double?
    @Published private(set) var isActive = false
    @Published private(set) var isCalibrating = false

    @Published private(set) var lastGesture: Gesture?
    @Published private(set) var gestureLog: [LogEntry] = []

    /// Chiamato sul main thread ogni volta che viene riconosciuto un gesto,
    /// come handle_gesture(gesture) in media_companion.py.
    var onGesture: ((Gesture) -> Void)?

    // --- parametri sonar, stessi default di doppler_hand.py ---
    private let blocksize = 2048            // dimensione richiesta al tap (come --blocksize)
    private let fftSize = 8192              // finestra grande / ring buffer (come --fft-size)
    private let gestureLen = 1024           // finestra piccola per l'onset (come --gesture-window)
    private let band: Double = 400          // larghezza banda attorno a f0 (come --band)

    private let ampThresholdOn = 1.5
    private let ampThresholdOff = 1.2
    private let debounceSamples = 2
    private let freqShiftThresholdOn = 14.0
    private let freqShiftThresholdOff = 8.0
    private let freqGateRatio = 1.1

    private let minTap: TimeInterval = 0.05
    private let holdTime: TimeInterval = 2.0
    private let doubleGap: TimeInterval = 0.4
    private let calibDuration: TimeInterval = 1.0
    private let stuckTimeout: TimeInterval = 3.0

    private let engine = AVAudioEngine()
    private var sourceNode: AVAudioSourceNode?

    // --- generazione tono (fase continua, come out_callback in Python) ---
    private var phase: Double = 0

    // --- analisi ---
    private var ring: [Float] = []
    private var inputSampleRate: Double = 48000
    private var smallAnalyzer: SpectrumAnalyzer?
    private var bigAnalyzer: SpectrumAnalyzer?

    private var smoothedAmp: Double?
    private var calibSamples: [Double] = []
    private var calibStart: TimeInterval?
    private var runningBaseline: Double?
    private var activeSince: TimeInterval?
    private var startTime: TimeInterval = 0
    private var warmupTime: TimeInterval = 0

    private var presence: PresenceDetector = PresenceDetector(thresholdOn: 1.5, thresholdOff: 1.2)
    private var detector: GestureDetector = GestureDetector(minTap: 0.05, holdTime: 2.0, doubleGap: 0.4)

    // MARK: - Controlli

    func start() {
        errorText = nil
        let session = AVAudioSession.sharedInstance()
        do {
            try session.setCategory(.playAndRecord, mode: sessionMode.avMode, options: [.defaultToSpeaker, .mixWithOthers])
            try session.setPreferredSampleRate(48000)
            try session.setActive(true)
        } catch {
            errorText = "Errore sessione audio: \(error.localizedDescription)"
            return
        }

        buildEngine()

        do {
            try engine.start()
            isRunning = true
            statusText = "In esecuzione (\(sessionMode.rawValue), \(Int(frequency)) Hz)"
            resetRuntimeState()
        } catch {
            errorText = "Errore avvio engine: \(error.localizedDescription)"
        }
    }

    func stop() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        if let sourceNode {
            engine.disconnectNodeOutput(sourceNode)
            engine.detach(sourceNode)
        }
        sourceNode = nil
        isRunning = false
        statusText = "Fermo"
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    func recalibrate() {
        resetRuntimeState()
    }

    func clearLog() {
        gestureLog = []
    }

    private func resetRuntimeState() {
        calibSamples = []
        calibStart = nil
        runningBaseline = nil
        smoothedAmp = nil
        activeSince = nil
        startTime = ProcessInfo.processInfo.systemUptime
        warmupTime = (Double(fftSize) / inputSampleRate) * 1.2
        presence = makePresenceDetector()
        detector = makeGestureDetector()

        DispatchQueue.main.async {
            self.baseline = nil
            self.isCalibrating = true
            self.isActive = false
            self.lastGesture = nil
        }
    }

    private func makePresenceDetector() -> PresenceDetector {
        PresenceDetector(thresholdOn: ampThresholdOn, thresholdOff: ampThresholdOff,
                          debounceSamples: debounceSamples,
                          freqThresholdOn: freqShiftThresholdOn, freqThresholdOff: freqShiftThresholdOff,
                          freqGateRatio: freqGateRatio)
    }

    private func makeGestureDetector() -> GestureDetector {
        GestureDetector(minTap: minTap, holdTime: holdTime, doubleGap: doubleGap)
    }

    // MARK: - Setup motore audio

    private func buildEngine() {
        engine.reset()
        phase = 0

        let sampleRate = AVAudioSession.sharedInstance().sampleRate
        guard let toneFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                              sampleRate: sampleRate,
                                              channels: 1,
                                              interleaved: false) else { return }

        let f0 = frequency
        let thetaIncrement = 2.0 * Double.pi * f0 / sampleRate
        var localPhase = phase

        let source = AVAudioSourceNode(format: toneFormat) { [weak self] _, _, frameCount, audioBufferList in
            let ablPointer = UnsafeMutableAudioBufferListPointer(audioBufferList)
            for frame in 0..<Int(frameCount) {
                let sampleValue = Float(sin(localPhase) * 0.5)
                localPhase += thetaIncrement
                if localPhase > 2.0 * Double.pi { localPhase -= 2.0 * Double.pi }
                for buffer in ablPointer {
                    let bufPtr = buffer.mData!.assumingMemoryBound(to: Float.self)
                    bufPtr[frame] = sampleValue
                }
            }
            self?.phase = localPhase
            return noErr
        }
        sourceNode = source
        engine.attach(source)
        engine.connect(source, to: engine.mainMixerNode, format: toneFormat)

        let inputFormat = engine.inputNode.outputFormat(forBus: 0)
        inputSampleRate = inputFormat.sampleRate

        ring = [Float](repeating: 0, count: fftSize)
        smallAnalyzer = SpectrumAnalyzer(size: gestureLen, sampleRate: inputSampleRate, centerFreq: f0, band: band)
        bigAnalyzer = SpectrumAnalyzer(size: fftSize, sampleRate: inputSampleRate, centerFreq: f0, band: band)

        engine.inputNode.installTap(onBus: 0, bufferSize: AVAudioFrameCount(blocksize), format: inputFormat) { [weak self] buffer, _ in
            self?.process(buffer: buffer)
        }

        engine.prepare()
    }

    // MARK: - Analisi (equivalente al corpo del while True: in doppler_hand.py)

    private func process(buffer: AVAudioPCMBuffer) {
        guard let channelData = buffer.floatChannelData?[0] else { return }
        let n = Int(buffer.frameLength)
        guard n > 0, let smallAnalyzer, let bigAnalyzer else { return }

        // Ring buffer: fa scorrere dentro i campioni nuovi, come ring[:-n] = ring[n:] in Python.
        if n >= fftSize {
            ring = Array(UnsafeBufferPointer(start: channelData + (n - fftSize), count: fftSize))
        } else {
            ring.removeFirst(n)
            ring.append(contentsOf: UnsafeBufferPointer(start: channelData, count: n))
        }

        let now = ProcessInfo.processInfo.systemUptime
        guard now - startTime >= warmupTime else { return } // il buffer non e' ancora pieno di audio reale

        // --- finestra piccola: ampiezza in banda per la soglia attivo/inattivo, bassa latenza ---
        let smallSlice = Array(ring.suffix(gestureLen))
        let (rawPeakAmpSmall, _) = smallAnalyzer.analyzeBandPeak(smallSlice)
        let smoothed = smoothedAmp.map { 0.5 * $0 + 0.5 * rawPeakAmpSmall } ?? rawPeakAmpSmall
        smoothedAmp = smoothed
        let peakAmp = smoothed

        // --- calibrazione baseline: mediana su calibDuration secondi, mano lontana ---
        if runningBaseline == nil {
            if calibStart == nil { calibStart = now }
            calibSamples.append(peakAmp)
            if now - calibStart! >= calibDuration {
                runningBaseline = calibSamples.sorted()[calibSamples.count / 2]
            }
            DispatchQueue.main.async {
                self.isCalibrating = true
                self.peakAmp = peakAmp
            }
            return
        }

        // --- finestra grande: frequenza di picco per lo shift Doppler (fusione in PresenceDetector) ---
        let (_, peakFrequency) = bigAnalyzer.analyzeBandPeak(ring)
        let deltaFrequency = peakFrequency - frequency

        let baselineValue = runningBaseline!
        let ratio = peakAmp / (baselineValue + 1e-9)
        let isActiveNow = presence.update(ampRatio: ratio, freqShiftHz: deltaFrequency)

        if !isActiveNow {
            runningBaseline = 0.98 * baselineValue + 0.02 * peakAmp
            activeSince = nil
        } else {
            if activeSince == nil {
                activeSince = now
            } else if now - activeSince! > stuckTimeout {
                // bloccato "attivo" troppo a lungo: probabile baseline partita male -> ricalibra al volo
                runningBaseline = peakAmp
                activeSince = nil
                presence = makePresenceDetector()
                detector = makeGestureDetector()
                appendLog("[ricalibrazione automatica: nessuna variazione per troppo tempo]")
                return
            }
        }

        let gesture = detector.update(isActive: isActiveNow, now: now)
        if let gesture {
            appendLog(gesture.rawValue)
        }

        DispatchQueue.main.async {
            self.isCalibrating = false
            self.baseline = self.runningBaseline
            self.peakAmp = peakAmp
            self.peakRatio = ratio
            self.deltaF = deltaFrequency
            self.isActive = isActiveNow
            if let gesture {
                self.lastGesture = gesture
                self.onGesture?(gesture)
            }
        }
    }

    private func appendLog(_ text: String) {
        DispatchQueue.main.async {
            self.gestureLog.append(LogEntry(time: Date(), text: text))
            if self.gestureLog.count > 30 {
                self.gestureLog.removeFirst(self.gestureLog.count - 30)
            }
        }
    }
}
