from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class Artifact:
    url: str
    sha256: str
    size: int
    name: str


@dataclass(frozen=True)
class Package:
    name: str
    version: str
    architecture: str
    suite: str
    component: str
    kind: str
    source: str
    source_version: str
    artifacts: tuple[Artifact, ...] = ()

    @property
    def slot(self):
        return canonical(
            [self.suite, self.component, self.kind, self.name, self.architecture]
        )

    @property
    def identity(self):
        return canonical([self.slot, self.version])

    @property
    def content(self):
        # Mirror paths can change without package contents changing.
        return sorted((a.name, a.sha256, a.size) for a in self.artifacts)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(
            **{**data, "artifacts": tuple(Artifact(**a) for a in data["artifacts"])}
        )


@dataclass
class Snapshot:
    packages: list[Package]
    releases: dict = field(default_factory=dict)


class GillsError(Exception):
    """An actionable error safe to expose in event history."""


class IntegrityError(GillsError):
    pass
