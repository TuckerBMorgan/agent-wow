"""Small result objects and attribute-accessible copies of observations."""
from dataclasses import dataclass, field


class Record(dict):
    def __getattribute__(self, name):
        if not name.startswith("_") and name in self:
            return dict.__getitem__(self, name)
        return dict.__getattribute__(self, name)


def record(value, *, cls=Record):
    if isinstance(value, dict):
        return cls({key: record(item, cls=cls) for key, item in dict.items(value)})
    if isinstance(value, list):
        return [record(item, cls=cls) for item in value]
    return value


@dataclass
class ActionResult:
    success: bool
    phase: str = "completed"
    reason: str | None = None
    message: str = ""
    data: dict = field(default_factory=dict)

    def __post_init__(self):
        self.data = record(self.data, cls=dict)

    def __getattr__(self, name):
        try:
            return record(self.data[name])
        except KeyError as error:
            raise AttributeError(name) from error


@dataclass
class RunResult:
    success: bool
    result: object = None
    error: str | None = None
    duration_s: float = 0
    final_state: dict | None = None
    logs: list = field(default_factory=list)


class SDKError(RuntimeError):
    def __init__(self, reason, message):
        self.reason = reason
        super().__init__(message)
