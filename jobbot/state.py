"""Encrypted memory of handled jobs, so nothing private sits in a public repo."""
import base64
import contextlib
import json
import os
import time

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

KEEP_DAYS = 240
MAX_RETRIES = 3  # how often one stopped form is tried again before it waits for the re-apply window


def _fernet(passphrase):
    kdf = Scrypt(salt=b"jobbot-state-v1", length=32, n=2 ** 15, r=8, p=1)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(str(passphrase).encode())))


def merge_jobs(ours, theirs):
    """Per job, the newer record wins (ties: ours). Nothing either side knows is lost."""
    out = dict(theirs)
    for key, v in ours.items():
        t = out.get(key)
        if t is None or v.get("ts", 0) >= t.get("ts", 0):
            out[key] = v
    return out


def merge_meta(ours, theirs):
    """Answers and caches are combined (ours win a conflict); the day's count keeps the higher one."""
    out = dict(theirs)
    for key, v in ours.items():
        t = out.get(key)
        if key == "day" and isinstance(v, dict) and isinstance(t, dict):
            if v.get("date") == t.get("date"):
                out[key] = dict(v, completed=max(int(v.get("completed", 0)), int(t.get("completed", 0))))
            else:
                out[key] = max(v, t, key=lambda d: d.get("date", ""))
        elif key == "unanswered" and isinstance(v, dict) and isinstance(t, dict):
            out[key] = {q: max(v.get(q, 0), t.get(q, 0)) for q in set(v) | set(t)}
        elif isinstance(v, dict) and isinstance(t, dict):
            out[key] = {**t, **v}
        else:
            out[key] = v
    return out


@contextlib.contextmanager
def _locked(path):
    """Only one process on this computer writes the state file at a time."""
    fh = open(path + ".lock", "a+")
    try:
        try:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)
        except ImportError:  # Windows
            import msvcrt
            fh.seek(0)
            while True:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.2)
        yield
    finally:
        fh.close()  # closing releases the lock


class State:
    def __init__(self, path, passphrase):
        self.path, self.f, self.jobs, self.meta = path, _fernet(passphrase), {}, {}
        data = self._read(path)
        if data:
            self.jobs, self.meta = data["jobs"], data.get("meta", {})

    def _read(self, path=None, blob=None):
        if blob is None:
            path = path or self.path
            if not (os.path.exists(path) and os.path.getsize(path)):
                return None
            with open(path, "rb") as fh:
                blob = fh.read()
        try:
            return json.loads(self.f.decrypt(blob))
        except InvalidToken:
            raise SystemExit("Cannot decrypt saved state: state_key changed. "
                             "Restore the old key or delete the bot-state branch to start over.")

    def merge(self, blob):
        """Fold another copy of the memory (encrypted bytes) into this one."""
        data = self._read(blob=blob) if blob else None
        if data:
            self.jobs = merge_jobs(self.jobs, data["jobs"])
            self.meta = merge_meta(self.meta, data.get("meta", {}))

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
            if (v["status"] in ("needs_answers", "verification", "rejected") and sig and v.get("sig") != sig
                    and v.get("tries", 1) < MAX_RETRIES):
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
        """Write the memory, merged with whatever another lane on this computer saved meanwhile."""
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with _locked(self.path):
            if os.path.exists(self.path) and os.path.getsize(self.path):
                with open(self.path, "rb") as fh:
                    self.merge(fh.read())
            cutoff = time.time() - KEEP_DAYS * 86400
            self.jobs = {k: v for k, v in self.jobs.items() if v["ts"] >= cutoff}
            tmp = f"{self.path}.{os.getpid()}.tmp"
            with open(tmp, "wb") as fh:
                fh.write(self.blob())
            os.replace(tmp, self.path)  # atomic: a crash never leaves a half-written file

    def blob(self):
        return self.f.encrypt(json.dumps({"jobs": self.jobs, "meta": self.meta}).encode())
