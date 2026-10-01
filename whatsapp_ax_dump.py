"""
Diagnostica: stampa l'albero di accessibilita' delle finestre di WhatsApp
(e dei banner di notifica) per scoprire come si chiama il pulsante per
rifiutare una chiamata in arrivo, prima di implementare il rifiuto in
media_companion.py.

Uso:
    python whatsapp_ax_dump.py              # aspetta 10 s, poi stampa
    python whatsapp_ax_dump.py --delay 20

Avvialo, fatti chiamare su WhatsApp e lascia squillare finche' non compare
l'output (salvato anche in whatsapp_ax_dump.txt). Serve il permesso di
Accessibilita' per il terminale (Impostazioni di Sistema -> Privacy e
sicurezza -> Accessibilita').
"""

import argparse
import subprocess
import sys
import time

# Visita ricorsiva degli elementi UI: una riga per elemento con ruolo, nome,
# descrizione e valore. Limitata in profondita' per non bloccarsi su liste lunghe.
_DUMP_SCRIPT = '''
on describe(el)
    tell application "System Events"
        set r to ""
        set n to ""
        set d to ""
        set v to ""
        try
            set r to role of el as text
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
    return r & " | name=" & n & " | desc=" & d & " | value=" & v
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

on dumpProcess(procName, maxDepth)
    tell application "System Events"
        if not (exists process procName) then return "== " & procName & ": processo non trovato" & linefeed
        set wins to windows of process procName
    end tell
    set out to "== " & procName & ": " & (count of wins) & " finestre" & linefeed
    repeat with w in wins
        set out to out & my dumpEl(w, 0, maxDepth)
    end repeat
    return out
end dumpProcess

on run argv
    set maxDepth to (item 1 of argv) as integer
    return my dumpProcess("WhatsApp", maxDepth) & linefeed & my dumpProcess("NotificationCenter", maxDepth)
end run
'''


def main():
    p = argparse.ArgumentParser(description="Stampa l'albero di accessibilita' di WhatsApp durante una chiamata")
    p.add_argument("--delay", type=float, default=10.0, help="Secondi di attesa prima della stampa (default 10)")
    p.add_argument("--max-depth", type=int, default=15, help="Profondita' massima dell'albero (default 15)")
    p.add_argument("--out", default="whatsapp_ax_dump.txt", help="File in cui salvare l'output")
    args = p.parse_args()

    print(f"Fatti chiamare su WhatsApp ora: stampo tra {args.delay:.0f} s, lascia squillare...")
    time.sleep(args.delay)

    result = subprocess.run(["osascript", "-", str(args.max_depth)], input=_DUMP_SCRIPT,
                            capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        print(f"Errore osascript: {result.stderr.strip()}", file=sys.stderr)
        print("Se parla di accesso/assistive access, concedi l'Accessibilita' al terminale.", file=sys.stderr)
        sys.exit(1)

    print(result.stdout)
    with open(args.out, "w") as f:
        f.write(result.stdout)
    print(f"Salvato in {args.out}")


if __name__ == "__main__":
    main()
