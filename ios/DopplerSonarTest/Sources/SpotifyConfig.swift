import Foundation

/// Valori da configurare a mano dopo aver registrato l'app su
/// https://developer.spotify.com/dashboard :
///   1. Crea una nuova app, copia il Client ID qui sotto.
///   2. Nelle impostazioni dell'app su Spotify, aggiungi in "Redirect URIs"
///      esattamente lo stesso valore di `redirectURI` qui sotto.
///   3. Lo scheme "dopplersonar" e' gia' registrato in Info.plist
///      (project.yml -> CFBundleURLTypes): se lo cambi, aggiornalo in entrambi i posti.
enum SpotifyConfig {
    static let clientID = "0bc98a7384f64ae59f3467c08f74e80a"
    static let redirectURI = URL(string: "dopplersonar://spotify-callback")!
}
