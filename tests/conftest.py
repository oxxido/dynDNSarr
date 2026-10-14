import importlib
from dataclasses import dataclass
from types import ModuleType

import pytest


@dataclass(frozen=True)
class Component:
    name: str
    env_prefix: str
    default_port: int
    default_data_dir: str
    db_filename: str

    def module(self, sub: str = "") -> ModuleType:
        return importlib.import_module(f"dyndnsarr_{self.name}{'.' + sub if sub else ''}")

    def settings(self, tmp_path, **overrides):
        return self.module("config").Settings(data_dir=tmp_path, **overrides)

    def app(self, settings):
        return self.module("main").create_app(settings)


COMPONENTS = [
    Component("agent", "DYNDNSARR_AGENT_", 8001, "/var/lib/dyndnsarr", "agent.db"),
]


@pytest.fixture(params=COMPONENTS, ids=lambda c: c.name)
def component(request) -> Component:
    return request.param
