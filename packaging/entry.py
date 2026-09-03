"""Entry point for the Windows build.

Two things differ from running `courseops` on a command line, and both exist
because of how a downloaded executable is actually used: double-clicked, from a
Downloads folder, by someone who has never opened a terminal.

  * With no arguments it serves, rather than printing usage and exiting. Usage
    text in a window that closes immediately helps nobody.

  * It holds the window open on the way out. A crash or a finished run would
    otherwise vanish along with whatever it was trying to tell you, which is
    indistinguishable from the program not working at all.
"""

import sys


def main() -> int:
    from courseops.cli import main as cli_main

    if len(sys.argv) == 1:
        sys.argv.append("serve")

    try:
        return cli_main()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    code = 0
    try:
        code = main()
    except SystemExit as exit_request:
        # argparse and our own startup checks exit this way, and their message
        # is the whole point of the run - it must not disappear.
        code = int(exit_request.code or 0)
        if exit_request.code:
            print(exit_request.code if isinstance(exit_request.code, str) else "")
    except Exception:
        import traceback

        traceback.print_exc()
        code = 1

    if sys.stdout.isatty():
        try:
            input("\nPress Enter to close this window. ")
        except (EOFError, KeyboardInterrupt):
            pass
    sys.exit(code)
