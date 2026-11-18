import os
import subprocess
import time

from jobbot import sync
from jobbot.models import Job
from jobbot.state import State, merge_meta


def job(i, source="greenhouse"):
    return Job(source, str(i), f"Co{i}", f"Engineer {i}", "Remote", "u", board=f"co{i}")


def test_two_lanes_on_one_computer_do_not_overwrite_each_other(tmp_path):
    path = str(tmp_path / "state.enc")
    a, b = State(path, "k"), State(path, "k")         # two lanes started from the same memory
    a.record(job(1), "submitted")
    a.meta.setdefault("learned", {})["Notice period?"] = "2 weeks"
    a.save()
    b.record(job(2, "workday"), "submitted")
    b.meta.setdefault("learned", {})["Favourite DB?"] = "Postgres"
    b.save()                                          # used to replace a's file wholesale
    final = State(path, "k")
    assert {job(1).key, job(2, "workday").key} <= set(final.jobs)
    assert final.meta["learned"] == {"Notice period?": "2 weeks", "Favourite DB?": "Postgres"}


def test_newest_record_wins_and_day_count_keeps_the_higher():
    ours = {"day": {"date": "2026-11-22", "completed": 40}, "unanswered": {"q": 2}, "inbox_issue": 3}
    theirs = {"day": {"date": "2026-11-22", "completed": 55}, "unanswered": {"q": 5, "r": 1}, "telegram_chat_id": 9}
    m = merge_meta(ours, theirs)
    assert m["day"]["completed"] == 55 and m["unanswered"] == {"q": 5, "r": 1}
    assert m["inbox_issue"] == 3 and m["telegram_chat_id"] == 9
    from jobbot.state import merge_jobs
    old, new = {"status": "manual", "ts": 1}, {"status": "submitted", "ts": 2}
    assert merge_jobs({"x": old}, {"x": new})["x"]["status"] == "submitted"
    assert merge_jobs({"x": new}, {"x": old})["x"]["status"] == "submitted"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_github_and_laptop_uploads_merge_instead_of_clobbering(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    clones = []
    for name in ("github", "laptop"):
        d = tmp_path / name
        _git(tmp_path, "init", "-q", str(d))
        _git(d, "remote", "add", "origin", str(remote))
        clones.append(d)
    lanes = {}
    for d, j in zip(clones, (job(1), job(2, "workday"))):
        monkeypatch.setattr(sync, "ROOT", str(d))
        p = str(d / ".state" / "state.enc")
        os.makedirs(os.path.dirname(p))
        sync.sync_down(p)                              # both start before either uploads
        s = State(p, "k")
        s.record(j, "submitted")
        s.save()
        lanes[d] = p
    for d, p in lanes.items():                         # both upload; the second must merge the first
        monkeypatch.setattr(sync, "ROOT", str(d))
        assert sync.sync_up("k", p)
    check = tmp_path / "check.enc"
    monkeypatch.setattr(sync, "ROOT", str(clones[0]))
    assert sync.sync_down(str(check))
    assert {job(1).key, job(2, "workday").key} <= set(State(str(check), "k").jobs)


def test_upload_is_refused_if_someone_uploaded_in_between(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    d = tmp_path / "lane"
    _git(tmp_path, "init", "-q", str(d))
    _git(d, "remote", "add", "origin", str(remote))
    monkeypatch.setattr(sync, "ROOT", str(d))
    p = str(d / "state.enc")
    s = State(p, "k")
    s.record(job(1), "submitted")
    s.save()
    assert sync.sync_up("k", p)
    real = sync.remote_head
    calls = []

    def stale():                                       # first look is stale: the lease must reject the push
        calls.append(1)
        sha, blob = real()
        return ("0" * 40, blob) if len(calls) == 1 else (sha, blob)
    monkeypatch.setattr(sync, "remote_head", stale)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert sync.sync_up("k", p) and len(calls) == 2
