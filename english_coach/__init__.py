from importlib import import_module

__all__ = ["agent"]


def __getattr__(name: str):
    if name == "agent":
        return import_module(f"{__name__}.agent")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
