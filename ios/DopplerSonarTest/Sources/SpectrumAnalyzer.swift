import Foundation
import Accelerate

/// FFT finestrata (Hann) su una banda attorno a f0, riusata sia per la
/// finestra "piccola" (onset veloce) sia per quella "grande" (stima Doppler),
/// come in doppler_hand.py.
final class SpectrumAnalyzer {
    let size: Int
    let binWidth: Double

    private let log2n: vDSP_Length
    private let fftSetup: FFTSetup
    private var hannWindow: [Float]
    private let bandBinRange: ClosedRange<Int>

    init?(size: Int, sampleRate: Double, centerFreq: Double, band: Double) {
        guard size > 1, (size & (size - 1)) == 0 else { return nil } // deve essere potenza di 2
        self.size = size
        self.binWidth = sampleRate / Double(size)
        self.log2n = vDSP_Length(log2(Double(size)))
        guard let setup = vDSP_create_fftsetup(log2n, FFTRadix(kFFTRadix2)) else { return nil }
        self.fftSetup = setup

        var window = [Float](repeating: 0, count: size)
        vDSP_hann_window(&window, vDSP_Length(size), Int32(vDSP_HANN_NORM))
        self.hannWindow = window

        let half = size / 2
        let lowBin = max(1, Int(((centerFreq - band) / binWidth).rounded(.down)))
        let highBin = min(half - 1, Int(((centerFreq + band) / binWidth).rounded(.up)))
        self.bandBinRange = lowBin...max(lowBin, highBin)
    }

    deinit {
        vDSP_destroy_fftsetup(fftSetup)
    }

    /// samples.count deve essere == size (ultima porzione del ring buffer).
    /// Ritorna l'ampiezza di picco in banda e la frequenza corrispondente.
    func analyzeBandPeak(_ samples: [Float]) -> (amplitude: Double, frequency: Double) {
        let half = size / 2
        var windowed = [Float](repeating: 0, count: size)
        vDSP_vmul(samples, 1, hannWindow, 1, &windowed, 1, vDSP_Length(size))

        var realp = [Float](repeating: 0, count: half)
        var imagp = [Float](repeating: 0, count: half)
        var magnitudes = [Float](repeating: 0, count: half)

        realp.withUnsafeMutableBufferPointer { realPtr in
            imagp.withUnsafeMutableBufferPointer { imagPtr in
                var splitComplex = DSPSplitComplex(realp: realPtr.baseAddress!, imagp: imagPtr.baseAddress!)
                windowed.withUnsafeBufferPointer { windowedPtr in
                    windowedPtr.baseAddress!.withMemoryRebound(to: DSPComplex.self, capacity: half) { complexPtr in
                        vDSP_ctoz(complexPtr, 2, &splitComplex, 1, vDSP_Length(half))
                    }
                }
                vDSP_fft_zrip(fftSetup, &splitComplex, 1, log2n, FFTDirection(FFT_FORWARD))
                vDSP_zvmags(&splitComplex, 1, &magnitudes, 1, vDSP_Length(half))
            }
        }

        var peakMagSquared: Float = 0
        var peakBin = bandBinRange.lowerBound
        for bin in bandBinRange where magnitudes[bin] > peakMagSquared {
            peakMagSquared = magnitudes[bin]
            peakBin = bin
        }

        let amplitude = Double(sqrtf(peakMagSquared))
        let frequency = Double(peakBin) * binWidth
        return (amplitude, frequency)
    }
}
