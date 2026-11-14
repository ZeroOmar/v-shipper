"""Print a bcrypt hash for web_ui.admin_password_hash.

Usage:
    docker run --rm ghcr.io/zeroomar/v-shipper hash-password 'your-password'
    docker run --rm -it ghcr.io/zeroomar/v-shipper hash-password      (prompts)
    python -m app.hash_password 'your-password'                       (local dev)
"""

import getpass
import sys

import bcrypt

ROUNDS = 12
MAX_BCRYPT_BYTES = 72  # bcrypt ignores everything past 72 bytes


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        print("usage: hash-password ['password']", file=sys.stderr)
        return 2

    if argv:
        password = argv[0]
    else:
        if not sys.stdin.isatty():
            password = sys.stdin.readline().rstrip("\r\n")
        else:
            password = getpass.getpass("Password: ")
            if getpass.getpass("Confirm:  ") != password:
                print("error: passwords do not match", file=sys.stderr)
                return 1

    if not password:
        print("error: password must not be empty", file=sys.stderr)
        return 1
    if len(password.encode("utf-8")) > MAX_BCRYPT_BYTES:
        print(f"error: password must be at most {MAX_BCRYPT_BYTES} bytes (bcrypt limit)", file=sys.stderr)
        return 1

    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=ROUNDS)).decode("ascii")
    print(hashed)
    if sys.stdout.isatty():
        print(f"\nIn docker-compose files escape every $ as $$:\n{hashed.replace('$', '$$')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
