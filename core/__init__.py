"""Core modules for the prediction market trading system."""


def __getattr__(name: str):
    """Lazy imports — only load submodules when actually accessed."""
    if name in ("Config", "get_config", "load_config"):
        from .config import Config, get_config, load_config

        globals().update(
            {"Config": Config, "get_config": get_config, "load_config": load_config}
        )
        return globals()[name]

    if name in ("EventBus", "Event"):
        from .events import Event, EventBus

        globals().update({"EventBus": EventBus, "Event": Event})
        return globals()[name]

    if name == "Database":
        from .storage import Database

        globals()["Database"] = Database
        return Database

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "Config",
    "get_config",
    "load_config",
    "EventBus",
    "Event",
    "Database",
]
