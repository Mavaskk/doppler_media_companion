import SwiftUI

struct ContentView: View {
    @StateObject private var engine = SonarEngine()
    @StateObject private var spotify = SpotifyController()
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        NavigationStack {
            Form {
                Section("Spotify") {
                    LabeledContent("Stato", value: spotify.statusText)
                    if let track = spotify.trackName, spotify.isConnected {
                        LabeledContent("Brano", value: track)
                    }
                    Button(spotify.isConnected ? "Connesso" : "Connetti a Spotify") {
                        spotify.connectTappingSpotify()
                    }
                    .disabled(spotify.isConnected)

                    if !spotify.isConfigured {
                        Text("Inserisci Client ID e Redirect URI in SpotifyConfig.swift (vedi commenti nel file).")
                            .font(.footnote)
                            .foregroundStyle(.orange)
                    }
                }

                Section("Sessione audio") {
                    Picker("Modalita'", selection: $engine.sessionMode) {
                        ForEach(SonarEngine.SessionMode.allCases) { mode in
                            Text(mode.rawValue).tag(mode)
                        }
                    }
                    .pickerStyle(.segmented)
                    .disabled(engine.isRunning)

                    Stepper(value: $engine.frequency, in: 15000...23000, step: 500) {
                        Text("Frequenza: \(Int(engine.frequency)) Hz")
                    }
                    .disabled(engine.isRunning)
                }

                Section {
                    Button(engine.isRunning ? "Ferma" : "Avvia") {
                        engine.isRunning ? engine.stop() : engine.start()
                    }
                    .tint(engine.isRunning ? .red : .green)

                    Button("Ricalibra baseline") {
                        engine.recalibrate()
                    }
                    .disabled(!engine.isRunning)
                }

                Section("Stato") {
                    LabeledContent("Stato", value: engine.statusText)
                    if let error = engine.errorText {
                        Text(error).foregroundStyle(.red)
                    }
                }

                if engine.isRunning {
                    Section("Lettura live") {
                        if engine.isCalibrating {
                            Text("Calibrazione in corso, tieni la mano lontana...")
                                .foregroundStyle(.secondary)
                        } else {
                            VStack(alignment: .leading, spacing: 6) {
                                Text("Ampiezza x\(engine.peakRatio, specifier: "%.2f")")
                                    .font(.title2.monospacedDigit())
                                    .foregroundStyle(engine.isActive ? .green : .primary)

                                GeometryReader { geo in
                                    RoundedRectangle(cornerRadius: 4)
                                        .fill(engine.isActive ? Color.green : Color.gray)
                                        .frame(width: min(CGFloat(engine.peakRatio) / 4.0, 1.0) * geo.size.width)
                                }
                                .frame(height: 16)

                                if engine.isActive {
                                    Text("MOVIMENTO RILEVATO")
                                        .font(.caption.bold())
                                        .foregroundStyle(.green)
                                }
                            }

                            LabeledContent("Baseline") {
                                Text(engine.baseline.map { String(format: "%.4g", $0) } ?? "-")
                            }
                            LabeledContent("Picco grezzo") {
                                Text(String(format: "%.4g", engine.peakAmp))
                            }
                            LabeledContent("\u{0394}f") {
                                Text(String(format: "%+.1f Hz", engine.deltaF))
                            }
                        }
                    }
                }

                if engine.isRunning, let gesture = engine.lastGesture {
                    Section("Ultimo gesto") {
                        Text(gesture.rawValue)
                            .font(.title.bold())
                            .foregroundStyle(.blue)
                            .frame(maxWidth: .infinity, alignment: .center)
                    }
                }

                if !engine.gestureLog.isEmpty {
                    Section {
                        ForEach(engine.gestureLog.reversed()) { entry in
                            HStack {
                                Text(entry.text)
                                Spacer()
                                Text(entry.time, style: .time)
                                    .foregroundStyle(.secondary)
                                    .font(.caption.monospacedDigit())
                            }
                        }
                    } header: {
                        HStack {
                            Text("Log gesti")
                            Spacer()
                            Button("Pulisci") { engine.clearLog() }
                                .font(.caption)
                        }
                    }
                }

                Section("Cosa guardare") {
                    Text("TAP SINGOLO: avvicina e allontana subito la mano -> play/pause. DOPPIO TAP: due contatti entro 0.4s -> traccia successiva. HOLD: tieni la mano vicina per 2s -> riavvia la traccia. Serve Spotify connesso e con una riproduzione gia' avviata.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Doppler Sonar Test")
        }
        .onAppear {
            engine.onGesture = { gesture in
                spotify.handle(gesture)
            }
        }
        .onOpenURL { url in
            spotify.handleOpenURL(url)
        }
        .onChange(of: scenePhase) { newPhase in
            switch newPhase {
            case .active:
                spotify.sceneDidBecomeActive()
            case .inactive, .background:
                spotify.sceneWillResignActive()
            @unknown default:
                break
            }
        }
    }
}

#Preview {
    ContentView()
}
