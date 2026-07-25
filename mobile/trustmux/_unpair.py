"""trustmux-unpair — list and remove paired devices."""
import json
import socket
import sys

from trustmux._ctl import Instance



def admin(cmd: dict, inst: Instance | None = None) -> object:
    inst = inst or Instance()
    if not inst.sock.exists():
        print("Error: Trustmux daemon not running (socket not found)", file=sys.stderr)
        sys.exit(1)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        try:
            s.connect(str(inst.sock))
        except OSError as e:
            print(f"Error: cannot connect to Trustmux daemon: {e}", file=sys.stderr)
            sys.exit(1)
        s.sendall(json.dumps(cmd).encode() + b"\n")
        s.shutdown(socket.SHUT_WR)
        s.settimeout(10)
        chunks = []
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except OSError as e:
            print(f"Error: timeout waiting for daemon response: {e}", file=sys.stderr)
            sys.exit(1)
        try:
            return json.loads(b"".join(chunks))
        except json.JSONDecodeError as e:
            print(f"Error: malformed response from daemon: {e}", file=sys.stderr)
            sys.exit(1)


def _ua_short(label: str) -> str:
    if not label:
        return "unknown"
    for keyword in ("Mobile", "Chrome", "Firefox", "Safari"):
        if keyword in label:
            return keyword
    return label[:30]


def main(inst: Instance | None = None):
    inst = inst or Instance()
    sessions = admin({"action": "sessions_list"}, inst)
    if not isinstance(sessions, list):
        print(f"Error: {sessions}", file=sys.stderr)
        sys.exit(1)

    if not sessions:
        print("No paired clients.")
        return

    print()
    print("Paired clients:")
    print()
    for i, s in enumerate(sessions, 1):
        ua = _ua_short(s.get("label", ""))
        print(f"  {i}.  {s['ip']:<20}  {ua:<12}  paired: {s['paired_at']}")
    print()
    print("  A  Remove all")
    print("  Q  Quit")
    print()

    try:
        choice = input(f"Select [1-{len(sessions)} / A / Q]: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return

    if not choice or choice.upper() == "Q":
        print("Cancelled.")

    elif choice.upper() == "A":
        result = admin({"action": "sessions_delete"}, inst)
        if isinstance(result, dict) and result.get("ok"):
            print(f"Removed {result.get('removed', 0)} paired client(s).")
        else:
            print(f"Error: {result}", file=sys.stderr)
            sys.exit(1)

    elif choice.isdigit() and 1 <= int(choice) <= len(sessions):
        idx = int(choice) - 1
        token = sessions[idx]["token_full"]
        ip = sessions[idx]["ip"]
        result = admin({"action": "sessions_delete", "token": token}, inst)
        if isinstance(result, dict) and result.get("ok"):
            print(f"Client {ip} unpaired.")
        else:
            print(f"Error: {result}", file=sys.stderr)
            sys.exit(1)

    else:
        print("Invalid selection.")
        sys.exit(1)


if __name__ == "__main__":
    main()
