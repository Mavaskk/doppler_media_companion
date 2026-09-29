import Foundation

/// Porta 1:1 di PresenceDetector in doppler_hand.py: fonde ampiezza e shift
/// Doppler in un booleano attivo/inattivo stabile (isteresi + OR + gate di
/// affidabilita' sul Doppler + debounce a campioni). Vedi doppler_hand.py per
/// la spiegazione estesa del perche' servono tutti e quattro i meccanismi.
final class PresenceDetector {
    let thresholdOn: Double
    let thresholdOff: Double
    let freqThresholdOn: Double?
    let freqThresholdOff: Double?
    let freqGateRatio: Double
    let debounceSamples: Int

    private(set) var isActive = false
    private var ampActive = false
    private var freqActive = false
    private var pendingCount = 0

    init(thresholdOn: Double, thresholdOff: Double, debounceSamples: Int = 2,
         freqThresholdOn: Double? = nil, freqThresholdOff: Double? = nil, freqGateRatio: Double = 1.1) {
        self.thresholdOn = thresholdOn
        self.thresholdOff = thresholdOff
        self.freqThresholdOn = freqThresholdOn
        self.freqThresholdOff = freqThresholdOff
        self.freqGateRatio = freqGateRatio
        self.debounceSamples = max(1, debounceSamples)
    }

    @discardableResult
    func update(ampRatio: Double, freqShiftHz: Double = 0) -> Bool {
        let ampThreshold = ampActive ? thresholdOff : thresholdOn
        ampActive = ampRatio > ampThreshold

        if let onThreshold = freqThresholdOn, ampRatio >= freqGateRatio {
            let offThreshold = freqThresholdOff ?? onThreshold
            let freqThreshold = freqActive ? offThreshold : onThreshold
            freqActive = abs(freqShiftHz) > freqThreshold
        } else {
            // Nessuna energia riflessa affidabile: non fidarsi dello shift stimato sul rumore.
            freqActive = false
        }

        let candidate = ampActive || freqActive
        if candidate != isActive {
            pendingCount += 1
            if pendingCount >= debounceSamples {
                isActive = candidate
                pendingCount = 0
            }
        } else {
            pendingCount = 0
        }

        return isActive
    }
}
