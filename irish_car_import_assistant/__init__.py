"""Public buyer integration; no backend, database or model-provider credentials."""

__all__ = ["compare_car"]


def __getattr__(name):
    if name == "compare_car":
        from .assistant import compare_car

        return compare_car
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
