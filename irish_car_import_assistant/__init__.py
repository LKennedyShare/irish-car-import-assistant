"""Public buyer integration; no backend, database or model-provider credentials."""

__all__ = ["compare_car", "CarImportTools"]


def __getattr__(name):
    if name == "CarImportTools":
        from .tools import CarImportTools

        return CarImportTools
    if name == "compare_car":
        from .assistant import compare_car

        return compare_car
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
