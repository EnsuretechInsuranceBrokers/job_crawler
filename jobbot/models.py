from dataclasses import dataclass, field


@dataclass
class Job:
    source: str            # greenhouse | lever | ashby | remotive | remoteok | arbeitnow
    id: str
    company: str
    title: str
    location: str
    url: str
    description: str = ""
    remote: bool = False
    posted: float = 0.0    # unix seconds, 0 = unknown
    board: str = ""        # ATS board token, when known
    score: float = 0.0
    named: bool = True     # False when `company` is only a board token, not a display name

    @property
    def key(self):
        return f"{self.source}:{self.board or self.company}:{self.id}"


@dataclass
class Outcome:
    status: str            # submitted | dry_run_ok | needs_answers | captcha | verification | unconfirmed | manual | error
    detail: str = ""
    unanswered: list = field(default_factory=list)
    llm_answers: list = field(default_factory=list)   # (question, answer) pairs to audit
