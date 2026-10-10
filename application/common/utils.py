"""Provide small helpers shared by the sample functions."""


def create_message(batch_name: str) -> str:
    """Return the confirmation message for one scheduled run."""
    return f"Hello CI from {batch_name}"
