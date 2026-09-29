"""Content checks that run before anything is stored.

* reject: the post is refused with a reason shown to the author.
* hold:   stored with status 'held' -- visible only to the author until a
          moderator restores it (先审后发 for risky content).
* allow:  stored as 'visible'.

The defaults target the two things that got anonymous apps taken down or
sued: doxxing of private individuals (phone / ID numbers) and defamation of
named people or companies. Replace or extend with a real classifier through
`ContentFilter.classifiers`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

Decision = str  # "allow" | "hold" | "reject"

# 11-digit mainland mobile numbers, 18-digit ID numbers, long digit runs.
PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("mobile_number", re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)")),
    ("id_number", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("bank_card", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
)

# Terms that put a post into the review queue instead of publishing it.
DEFAULT_HOLD_TERMS: tuple[str, ...] = (
    "身份证号", "家庭住址", "微信号", "手机号是", "开房", "出轨", "小三", "人渣",
    "曝光", "爆料", "内部消息", "内幕", "裁员名单", "工资单", "举报他",
)


@dataclass
class Verdict:
    decision: Decision
    reasons: list[str] = field(default_factory=list)


class ContentFilter:
    def __init__(
        self,
        hold_terms: Iterable[str] = DEFAULT_HOLD_TERMS,
        reject_terms: Iterable[str] = (),
        classifiers: Iterable[Callable[[str], Verdict | None]] = (),
    ) -> None:
        self.hold_terms = tuple(t for t in hold_terms if t)
        self.reject_terms = tuple(t for t in reject_terms if t)
        self.classifiers = list(classifiers)

    @classmethod
    def from_file(cls, path: str | Path) -> "ContentFilter":
        """One term per line. Lines starting with '!' are reject terms, all
        others are hold terms. '#' starts a comment."""
        hold, reject = list(DEFAULT_HOLD_TERMS), []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            (reject if line.startswith("!") else hold).append(line.lstrip("!"))
        return cls(hold_terms=hold, reject_terms=reject)

    def check(self, text: str) -> Verdict:
        reasons: list[str] = []
        for label, pattern in PII_PATTERNS:
            if pattern.search(text):
                reasons.append(f"pii:{label}")
        for term in self.reject_terms:
            if term in text:
                reasons.append(f"term:{term}")
        if reasons:
            return Verdict("reject", reasons)
        for classifier in self.classifiers:
            verdict = classifier(text)
            if verdict is not None and verdict.decision != "allow":
                return verdict
        held = [f"term:{t}" for t in self.hold_terms if t in text]
        if held:
            return Verdict("hold", held)
        return Verdict("allow")


def normalise_body(text: str, max_len: int) -> str:
    text = text.replace("\r\n", "\n").strip()
    if not text:
        raise ValueError("内容不能为空")
    if len(text) > max_len:
        raise ValueError(f"内容超过 {max_len} 字")
    return text
