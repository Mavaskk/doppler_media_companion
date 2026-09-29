import Foundation
import SpotifyiOS

/// Traduce i gesti (TAP SINGOLO / DOPPIO TAP / HOLD) in comandi verso l'app
/// Spotify via App Remote SDK. Equivalente iOS delle funzioni spotify_* in
/// media_companion.py, che li' usavano AppleScript (non disponibile su iOS).
final class SpotifyController: NSObject, ObservableObject {

    private static let accessTokenDefaultsKey = "spotify-access-token"

    @Published private(set) var isConnected = false
    @Published private(set) var statusText = "Non connesso"
    @Published private(set) var trackName: String?
    @Published private(set) var isPaused = true

    private var accessToken: String? {
        didSet { UserDefaults.standard.set(accessToken, forKey: Self.accessTokenDefaultsKey) }
    }

    private lazy var appRemote: SPTAppRemote = {
        let configuration = SPTConfiguration(clientID: SpotifyConfig.clientID, redirectURL: SpotifyConfig.redirectURI)
        let remote = SPTAppRemote(configuration: configuration, logLevel: .debug)
        remote.connectionParameters.accessToken = accessToken
        remote.delegate = self
        return remote
    }()

    override init() {
        super.init()
        accessToken = UserDefaults.standard.string(forKey: Self.accessTokenDefaultsKey)
    }

    var isConfigured: Bool {
        SpotifyConfig.clientID != "INSERISCI_IL_TUO_CLIENT_ID" && !SpotifyConfig.clientID.isEmpty
    }

    // MARK: - Connessione

    /// Da chiamare quando l'utente preme "Connetti a Spotify". Apre l'app Spotify
    /// per l'autorizzazione (e avvia la riproduzione dell'ultimo brano/uno casuale
    /// se non stava gia' suonando nulla: serve perche' App Remote richiede una
    /// riproduzione attiva per restare connesso in background).
    func connectTappingSpotify() {
        guard isConfigured else {
            statusText = "Configura Client ID e Redirect URI in SpotifyConfig.swift"
            return
        }
        appRemote.authorizeAndPlayURI("")
    }

    /// Da collegare a `.onOpenURL` nella view principale: legge il token dal redirect.
    func handleOpenURL(_ url: URL) {
        let parameters = appRemote.authorizationParameters(from: url)
        if let token = parameters?[SPTAppRemoteAccessTokenKey] {
            accessToken = token
            appRemote.connectionParameters.accessToken = token
            appRemote.connect()
        } else if let errorDescription = parameters?[SPTAppRemoteErrorDescriptionKey] {
            statusText = "Errore autorizzazione: \(errorDescription)"
        }
    }

    /// Da chiamare quando l'app torna in primo piano.
    func sceneDidBecomeActive() {
        guard isConfigured, accessToken != nil, !appRemote.isConnected else { return }
        appRemote.connect()
    }

    /// Da chiamare quando l'app va in background: buona norma disconnettersi
    /// per lasciare a Spotify la certezza di poter sospendere lo stream.
    func sceneWillResignActive() {
        if appRemote.isConnected {
            appRemote.disconnect()
        }
    }

    // MARK: - Comandi (equivalenti a spotify_play_pause / spotify_next / spotify_restart)

    func handle(_ gesture: Gesture) {
        guard isConnected else { return }
        switch gesture {
        case .tapSingolo:
            if isPaused {
                appRemote.playerAPI?.resume(nil)
            } else {
                appRemote.playerAPI?.pause(nil)
            }
        case .doppioTap:
            appRemote.playerAPI?.skip(toNext: nil)
        case .hold:
            appRemote.playerAPI?.seek(toPosition: 0, callback: nil)
        }
    }
}

// MARK: - SPTAppRemoteDelegate

extension SpotifyController: SPTAppRemoteDelegate {
    func appRemoteDidEstablishConnection(_ appRemote: SPTAppRemote) {
        isConnected = true
        statusText = "Connesso a Spotify"
        appRemote.playerAPI?.delegate = self
        appRemote.playerAPI?.subscribe(toPlayerState: { [weak self] _, error in
            if let error {
                self?.statusText = "Errore subscribe player state: \(error.localizedDescription)"
            }
        })
    }

    func appRemote(_ appRemote: SPTAppRemote, didFailConnectionAttemptWithError error: Error?) {
        isConnected = false
        statusText = "Connessione fallita: \(error?.localizedDescription ?? "sconosciuto")"
    }

    func appRemote(_ appRemote: SPTAppRemote, didDisconnectWithError error: Error?) {
        isConnected = false
        statusText = error == nil ? "Disconnesso" : "Disconnesso: \(error!.localizedDescription)"
    }
}

// MARK: - SPTAppRemotePlayerStateDelegate

extension SpotifyController: SPTAppRemotePlayerStateDelegate {
    func playerStateDidChange(_ playerState: SPTAppRemotePlayerState) {
        isPaused = playerState.isPaused
        trackName = playerState.track.name
    }
}
