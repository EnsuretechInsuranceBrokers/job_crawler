"""Keep the profile in the (public) repository, encrypted.

    python -m jobbot.seal new-key            # print a fresh key -> store it as the JOBBOT_KEY secret
    python -m jobbot.seal seal               # profile.yml -> profile.enc (commit this one)
    python -m jobbot.seal open               # profile.enc -> profile.yml (to edit it again)

profile.enc is AES-encrypted and authenticated (Fernet). Without the key it is
noise, so it is safe to commit; profile.yml itself stays git-ignored. The key is
read from the JOBBOT_KEY environment variable.
"""
import os
import sys

from cryptography.fernet import Fernet, InvalidToken

from .config import ROOT

PLAIN, SEALED = os.path.join(ROOT, "profile.yml"), os.path.join(ROOT, "profile.enc")


def unseal(key, path=SEALED):
    with open(path, "rb") as f:
        try:
            return Fernet(key.strip().encode()).decrypt(f.read()).decode()
        except (InvalidToken, ValueError):
            raise SystemExit("JOBBOT_KEY does not open profile.enc (wrong key, or the file was sealed with another one).")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "new-key":
        print(Fernet.generate_key().decode())
        return
    key = os.environ.get("JOBBOT_KEY", "").strip()
    if cmd not in ("seal", "open") or not key:
        raise SystemExit(__doc__)
    if cmd == "seal":
        with open(PLAIN, "rb") as f:
            data = f.read()
        import yaml
        yaml.safe_load(data)  # refuse to seal something the bot could not read
        with open(SEALED, "wb") as f:
            f.write(Fernet(key.encode()).encrypt(data))
        print("wrote profile.enc - commit it")
    else:
        with open(PLAIN, "w") as f:
            f.write(unseal(key))
        print("wrote profile.yml - edit, then seal again")


if __name__ == "__main__":
    main(sys.argv)
