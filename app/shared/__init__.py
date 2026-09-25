"""Shared Module - Componenti condivisi tra i servizi."""

__all__ = [
    "get_model",
    "SeeristRetriever",
    "ReliefWebRetriever",
]


def __getattr__(name: str):
    if name == "get_model":
        from .llm import get_model

        return get_model
    if name in {"SeeristRetriever", "ReliefWebRetriever"}:
        from .retrievers import ReliefWebRetriever, SeeristRetriever

        return {
            "SeeristRetriever": SeeristRetriever,
            "ReliefWebRetriever": ReliefWebRetriever,
        }[name]
    raise AttributeError(name)
