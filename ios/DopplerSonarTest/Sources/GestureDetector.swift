import Foundation

enum Gesture: String {
    case tapSingolo = "TAP SINGOLO"
    case doppioTap = "DOPPIO TAP"
    case hold = "HOLD"
}

private enum GestureState {
    case idle          // mano lontana, nessun contatto in corso
    case present       // mano vicina (primo o secondo contatto)
    case waitSecond    // mano appena ritirata dopo il primo contatto: in attesa di un eventuale secondo
}

/// Porta 1:1 di GestureDetector in doppler_hand.py: state machine esplicita a
/// timer sulla sequenza pulita di presenza/assenza prodotta da PresenceDetector.
///   - si ritira subito e non torna         -> TAP SINGOLO
///   - si ritira e torna entro doubleGap    -> DOPPIO TAP
///   - resta presente per >= holdTime       -> HOLD
final class GestureDetector {
    let minTap: TimeInterval
    let holdTime: TimeInterval
    let doubleGap: TimeInterval

    private var state: GestureState = .idle
    private var presentStart: TimeInterval?
    private var tapIndex = 0        // 1 = primo contatto, 2 = secondo (candidato doppio tap)
    private var holdReported = false
    private var releaseTime: TimeInterval?

    init(minTap: TimeInterval, holdTime: TimeInterval, doubleGap: TimeInterval) {
        self.minTap = minTap
        self.holdTime = holdTime
        self.doubleGap = doubleGap
    }

    /// Chiamata a ogni ciclo di analisi. Ritorna il gesto rilevato, o nil.
    func update(isActive: Bool, now: TimeInterval) -> Gesture? {
        var gesture: Gesture?

        switch state {
        case .idle:
            if isActive {
                enterPresent(tapIndex: 1, now: now)
            }

        case .waitSecond:
            if isActive {
                enterPresent(tapIndex: 2, now: now)
            } else if now - (releaseTime ?? now) > doubleGap {
                // nessun secondo contatto arrivato in tempo -> era un tap singolo
                gesture = .tapSingolo
                state = .idle
            }

        case .present:
            let duration = now - (presentStart ?? now)
            if isActive {
                if duration >= holdTime && !holdReported {
                    gesture = .hold
                    holdReported = true
                }
            } else {
                if holdReported {
                    state = .idle // gia' segnalato durante la presenza
                } else if duration < minTap {
                    state = .idle // troppo breve, probabile rumore residuo
                } else if tapIndex == 1 {
                    state = .waitSecond
                    releaseTime = now
                } else { // tapIndex == 2
                    gesture = .doppioTap
                    state = .idle
                }
            }
        }

        return gesture
    }

    private func enterPresent(tapIndex: Int, now: TimeInterval) {
        state = .present
        presentStart = now
        self.tapIndex = tapIndex
        holdReported = false
    }
}
