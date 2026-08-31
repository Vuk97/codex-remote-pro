"""Fake interactive PTY target that behaves like a chat CLI.

Prints a ready banner and a prompt, reads lines, echoes each received line
as `echo:<line>` (so tests can assert exact, exactly-once delivery), strips
bracketed-paste markers the way a paste-aware TUI would, prints INTERRUPTED
on Ctrl-C, and exits on the line `quit`.
"""

import signal
import sys


def main() -> int:
    signal.signal(signal.SIGINT, lambda *_: print("INTERRUPTED", flush=True))
    print("FAKE-REPL-READY", flush=True)
    print("prompt> ", end="", flush=True)
    for line in sys.stdin:
        line = line.rstrip("\n").rstrip("\r")
        line = line.replace("\x1b[200~", "").replace("\x1b[201~", "")
        if line == "quit":
            print("FAKE-REPL-BYE", flush=True)
            return 0
        print(f"echo:{line}", flush=True)
        print("prompt> ", end="", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
