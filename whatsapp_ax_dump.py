"""
Diagnostica: scopre dove compare la UI di una chiamata WhatsApp in arrivo
(finestra di WhatsApp, processo helper o banner di notifica) e come si
chiama il pulsante per rifiutarla, prima di implementare il rifiuto in
media_companion.py.

Uso:
    python whatsapp_ax_dump.py                 # osserva per 60 s
    python whatsapp_ax_dump.py --duration 90

Avvialo, fatti chiamare su WhatsApp e lascia squillare. Ogni secondo lo script
legge l'elenco delle finestre dal window server (vede anche quelle su altri
Space, senza permessi); quando cambia, stampa le differenze e l'albero di
accessibilita' dei processi coinvolti. Tutto viene salvato anche in
whatsapp_ax_dump.txt.

L'albero di accessibilita' richiede il permesso di Accessibilita' per il
terminale (Impostazioni di Sistema -> Privacy e sicurezza -> Accessibilita')
e vede solo le finestre dello Space corrente: tieni WhatsApp sullo stesso
Space del terminale, non a schermo intero.
"""

import argparse
import subprocess
import time

WATCHED_OWNERS = ("WhatsApp", "WAAppKit", "Notification")

# Elenco finestre dal window server: una riga per finestra dei processi osservati.
_CG_WINDOWS_JXA = r'''
ObjC.import('CoreGraphics');
var arr = ObjC.deepUnwrap(ObjC.castRefToObject($.CGWindowListCopyWindowInfo($.kCGWindowListOptionAll, 0)));
arr.filter(function(w){ return /%s/.test(w.kCGWindowOwnerName || ''); })
   .map(function(w){ var b = w.kCGWindowBounds;
        return (w.kCGWindowOwnerName || '').replace(/‎/g, '') + ' pid=' + w.kCGWindowOwnerPID +
               ' id=' + w.kCGWindowNumber + ' layer=' + w.kCGWindowLayer +
               ' onscreen=' + (w.kCGWindowIsOnscreen ? 1 : 0) +
               ' ' + Math.round(b.Width) + 'x' + Math.round(b.Height) +
               ' @' + Math.round(b.X) + ',' + Math.round(b.Y); })
   .join('\n');
''' % "|".join(WATCHED_OWNERS)

_AX_TRUSTED_JXA = 'ObjC.import("ApplicationServices"); $.AXIsProcessTrusted()'

# Albero di accessibilita' dei processi indicati per PID: una riga per
# elemento con ruolo, sottoruolo, nome, descrizione e valore.
_AX_DUMP_SCRIPT = '''
on describe(el)
    tell application "System Events"
        set r to ""
        set sr to ""
        set n to ""
        set d to ""
        set v to ""
        try
            set r to role of el as text
        end try
        try
            set sr to subrole of el as text
        end try
        try
            set n to name of el as text
        end try
        try
            set d to description of el as text
        end try
        try
            set v to value of el as text
        end try
    end tell
    return r & "/" & sr & " | name=" & n & " | desc=" & d & " | value=" & v
end describe

on dumpEl(el, depth, maxDepth)
    set indent to ""
    repeat depth times
        set indent to indent & "  "
    end repeat
    set out to indent & my describe(el) & linefeed
    if depth < maxDepth then
        tell application "System Events"
            set kids to {}
            try
                set kids to UI elements of el
            end try
        end tell
        repeat with k in kids
            set out to out & my dumpEl(k, depth + 1, maxDepth)
        end repeat
    end if
    return out
end dumpEl

on run argv
    set maxDepth to (item 1 of argv) as integer
    set out to ""
    repeat with i from 2 to count of argv
        set thePid to (item i of argv) as integer
        try
            tell application "System Events"
                set p to first process whose unix id is thePid
                set pName to name of p
                set wins to windows of p
            end tell
            set out to out & "== " & pName & " (pid " & thePid & "): " & (count of wins) & " finestre AX" & linefeed
            repeat with w in wins
                set out to out & my dumpEl(w, 0, maxDepth)
            end repeat
        on error e number num
            set out to out & "== pid " & thePid & ": errore " & num & " " & e & linefeed
        end try
    end repeat
    return out
end run
'''


def _run(cmd, stdin=None, timeout=60):
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "", "timeout"
    return r.stdout.strip(), r.stderr.strip()


def cg_windows():
    out, _ = _run(["osascript", "-l", "JavaScript", "-e", _CG_WINDOWS_JXA])
    return set(line for line in out.splitlines() if line)


def ax_dump(pids, max_depth):
    out, err = _run(["osascript", "-", str(max_depth), *map(str, pids)], stdin=_AX_DUMP_SCRIPT)
    return out + (f"\n[stderr] {err}" if err else "")


def main():
    p = argparse.ArgumentParser(description="Osserva le finestre di WhatsApp durante una chiamata in arrivo")
    p.add_argument("--duration", type=float, default=60.0, help="Secondi di osservazione (default 60)")
    p.add_argument("--max-depth", type=int, default=12, help="Profondita' massima dell'albero AX (default 12)")
    p.add_argument("--out", default="whatsapp_ax_dump.txt", help="File in cui salvare l'output")
    args = p.parse_args()

    log = open(args.out, "w")

    def emit(text):
        print(text)
        log.write(text + "\n")
        log.flush()

    trusted, _ = _run(["osascript", "-l", "JavaScript", "-e", _AX_TRUSTED_JXA])
    emit(f"Accessibilita' concessa al terminale: {trusted}")
    if trusted != "true":
        emit("  -> senza questo permesso l'albero AX resta vuoto: concedilo e rilancia.")

    prev = cg_windows()
    emit(f"\n--- finestre iniziali ({len(prev)}) ---")
    emit("\n".join(sorted(prev)) or "(nessuna)")
    pids = sorted({line.split("pid=")[1].split()[0] for line in prev})
    emit("\n--- albero AX iniziale ---")
    emit(ax_dump(pids, max_depth=2))

    emit(f"\nOsservo per {args.duration:.0f} s: fatti chiamare su WhatsApp ora e lascia squillare...")
    start = time.monotonic()
    while time.monotonic() - start < args.duration:
        time.sleep(1.0)
        cur = cg_windows()
        if cur == prev:
            continue
        t = time.monotonic() - start
        emit(f"\n=== t={t:.0f}s: finestre cambiate ===")
        for line in sorted(cur - prev):
            emit(f"+ {line}")
        for line in sorted(prev - cur):
            emit(f"- {line}")
        changed_pids = sorted({line.split("pid=")[1].split()[0] for line in cur ^ prev})
        emit(ax_dump(changed_pids, max_depth=args.max_depth))
        prev = cur

    emit(f"\nFine. Output salvato in {args.out}")
    log.close()


if __name__ == "__main__":
    main()
