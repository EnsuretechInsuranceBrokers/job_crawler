"""Share the bot's encrypted memory (the `bot-state` branch) between every lane.

The GitHub bot and the commands on your computer can run at the same time: each
upload first merges in what the others uploaded, then replaces the branch only if
nobody uploaded in between (compare-and-swap); otherwise it merges again and retries.

    python -m jobbot.sync pull        # fetch the memory into .state/state.enc
    python -m jobbot.sync push        # merge with the uploaded memory and upload
"""
import os
import subprocess
import sys
import time

from .config import ROOT

STATE = os.path.join(ROOT, ".state", "state.enc")
BRANCH = "bot-state"


def _git(*args, inp=None, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd or ROOT, input=inp, capture_output=True)


def _remote():
    return os.environ.get("JOBBOT_STATE_REMOTE") or "origin"


def remote_head():
    """(sha of the uploaded memory or "" if there is none yet, encrypted bytes or None). None sha = unreachable."""
    r = _git("ls-remote", "--heads", _remote(), BRANCH)
    if r.returncode != 0:
        return None, None
    line = r.stdout.decode().strip()
    if not line:
        return "", None
    sha = line.split()[0]
    if _git("fetch", "-q", "--depth=1", _remote(), f"+refs/heads/{BRANCH}:refs/jobbot/{BRANCH}").returncode != 0:
        return None, None
    blob = _git("show", f"{sha}:state.enc")
    return sha, (blob.stdout if blob.returncode == 0 and blob.stdout else None)


def sync_down(path=None):
    path = path or STATE
    sha, blob = remote_head()
    if not blob:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(blob)
    return True


def _commit(files):
    """A single parentless commit holding `files` {name: bytes}; returns its sha."""
    entries = []
    for name, data in sorted(files.items()):
        h = _git("hash-object", "-w", "--stdin", inp=data)
        if h.returncode != 0:
            raise RuntimeError("git hash-object failed")
        entries.append(f"100644 blob {h.stdout.decode().strip()}\t{name}\n")
    tree = _git("mktree", inp="".join(entries).encode()).stdout.decode().strip()
    env = dict(os.environ, GIT_AUTHOR_NAME="jobbot", GIT_AUTHOR_EMAIL="jobbot@users.noreply.github.com",
               GIT_COMMITTER_NAME="jobbot", GIT_COMMITTER_EMAIL="jobbot@users.noreply.github.com")
    r = subprocess.run(["git", "commit-tree", tree, "-m", "encrypted state"], cwd=ROOT, capture_output=True, env=env)  # ROOT read at call time
    if r.returncode != 0:
        raise RuntimeError("git commit-tree failed")
    return r.stdout.decode().strip()


def sync_up(passphrase, path=None, tries=6):
    """Merge the uploaded memory into the local one and upload the result. True on success."""
    from .state import State
    path = path or STATE
    if not os.path.exists(path):
        return False
    for attempt in range(tries):
        sha, blob = remote_head()
        if sha is None:
            time.sleep(2 + attempt * 3)
            continue
        state = State(path, passphrase)
        state.merge(blob)
        state.save()                        # the local file now holds everything both sides know
        with open(path, "rb") as fh:
            files = {"state.enc": fh.read()}
        badge = os.path.join(os.path.dirname(path), "badge.json")
        if os.path.exists(badge):
            with open(badge, "rb") as fh:
                files["badge.json"] = fh.read()
        commit = _commit(files)
        r = _git("push", "-q", f"--force-with-lease=refs/heads/{BRANCH}:{sha}", _remote(), f"{commit}:refs/heads/{BRANCH}")
        if r.returncode == 0:
            return True
        time.sleep(1 + attempt * 2)         # someone uploaded in between: merge their copy too and retry
    return False


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["pull"]:
        ok = sync_down()
        print("memory fetched" if ok else "no saved memory yet")
        return 0
    if argv[:1] == ["push"]:
        from .config import load_profile
        ok = sync_up(load_profile()["state_key"])
        print("memory saved" if ok else "could not save the memory")
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
