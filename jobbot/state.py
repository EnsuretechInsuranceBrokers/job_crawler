"""Encrypted memory of handled jobs, so nothing private sits in a public repo."""
import base64
import json
import os
import time

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

KEEP_DAYS = 240


def _fernet(passphrase):
    kdf = Scrypt(salt=b"jobbot-state-v1", length=32, n=2 ** 15, r=8, p=1)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(str(passphrase).encode())))


class State:
    def __init__(self, path, passphrase):
        self.path, self.f, self.jobs, self.meta = path, _fernet(passphrase), {}, {}
        if os.path.exists(path) and os.path.getsize(path):
            try:
                with open(path, "rb") as fh:
                    data = json.loads(self.f.decrypt(fh.read()))
                self.jobs, self.meta = data["jobs"], data.get("meta", {})
            except InvalidToken:
                raise SystemExit("Cannot decrypt saved state: state_key changed. "
                                 "Restore the old key or delete the bot-state branch to start over.")

    def seen(self, job, reapply_days=0, sig=None):
        """Has this job (or the same role elsewhere) been handled?

        With reapply_days > 0 a job becomes eligible again once that many days have
        passed since it was last handled; 0 means never apply twice.
        """
        from .match import role_key
        now = time.time()

        def blocks(v):
            if v["status"] == "error" and v.get("tries", 1) < 2:
                return False  # errors get one retry on a later run
            if v["status"] in ("needs_answers", "verification", "rejected") and sig and v.get("sig") != sig:
                return False  # the bot can do more than when this form stopped (new answers, a fix): try it again
            return not reapply_days or now - v["ts"] < reapply_days * 86400

        if job.key in self.jobs:
            return blocks(self.jobs[job.key])
        role = role_key(job)
        return any(v.get("role") == role and blocks(v) for v in self.jobs.values())

    def record(self, job, status, sig=None):
        """Remember a job. "attempting" is written BEFORE a form is submitted, so a run that
        dies mid-application can never apply to the same job again on the next run."""
        from .match import role_key
        tries = self.jobs.get(job.key, {}).get("tries", 0) + (status != "attempting")
        prev = self.jobs.get(job.key, {})
        if prev.get("status") not in (None, "attempting", "error") and status == "attempting":
            tries = 0  # a new weekly round starts
        self.jobs[job.key] = {"status": status, "ts": int(time.time()), "role": role_key(job), "tries": tries,
                              "rounds": prev.get("rounds", 0) + (status == "submitted"), "sig": sig,
                              "title": job.title, "company": job.company, "url": job.url}

    def save(self):
        cutoff = time.time() - KEEP_DAYS * 86400
        self.jobs = {k: v for k, v in self.jobs.items() if v["ts"] >= cutoff}
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(self.f.encrypt(json.dumps({"jobs": self.jobs, "meta": self.meta}).encode()))
        os.replace(tmp, self.path)  # atomic: a crash never leaves a half-written file
