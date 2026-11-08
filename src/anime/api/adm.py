"""Local privileged password recovery through the API's Unix socket."""
import argparse
import getpass
import json
import socket
import sys


def main():
    if sys.argv[1:2] == ["report"]:
        # Read-only diagnostics straight from the database; the API need not run.
        from src.anime.job_report import main as report
        sys.exit(report(sys.argv[2:]))
    if sys.argv[1:2] == ["recover"]:
        # Stopped job diagnostics, classification, and safe recovery.
        from src.anime.job_recovery import main as recover
        sys.exit(recover(sys.argv[2:]))
    if sys.argv[1:2] == ["services"]:
        # One-off check of every external service; AniDB is read from cache.
        from src.anime.services.check import main as services
        sys.exit(services(sys.argv[2:]))
    parser = argparse.ArgumentParser(
        description="Anime Discovery local administration",
        epilog="Also: adm report ... (read-only job reports) | adm recover ... (historical job recovery & retry, including adm recover resv --ep-id N [--release] and batch-audit) | adm services check (external service health)",
    )
    parser.add_argument("command", choices=("set-password",))
    parser.add_argument("--socket", default="/run/anime-discovery/admin.sock")
    parser.add_argument("--password-stdin", action="store_true", help="Read a password from standard input for automation")
    parser.add_argument("--if-unset", action="store_true", help="Initialize only; preserve an existing password")
    args = parser.parse_args()
    if args.password_stdin:
        password = sys.stdin.buffer.read(1026).decode('utf-8').removesuffix('\n')
    else:
        password = getpass.getpass("New administrator password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            parser.error("Passwords do not match")
    from src.anime.api.passwords import validate_password
    try:
        validate_password(password)
    except ValueError:
        parser.error("Password must be 12–256 characters with no control characters")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(15)
        conn.connect(args.socket)
        request = {"command": args.command, "password": password}
        if args.if_unset:request["if_unset"] = True
        payload = json.dumps(request).encode()
        conn.sendall(payload + bytes([10]))
        response = b""
        while not response.endswith(bytes([10])) and len(response) < 2048:
            part = conn.recv(2048)
            if not part:
                break
            response += part
    try:
        result = json.loads(response)
    except (ValueError, UnicodeError):
        parser.error("Invalid admin socket response")
    if result.get("ok") is not True:
        parser.error("Password change failed")
    if result.get("unchanged"):
        print("Existing administrator password preserved.")
        return
    print("Administrator password updated; existing dashboard sessions are invalidated.")


if __name__ == "__main__":
    main()
