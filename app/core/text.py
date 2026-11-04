"""Normalize user-facing text before passing it to PostgreSQL or templates."""

from typing import Annotated

from pydantic import BeforeValidator


def database_text(value: object) -> object:
    if not isinstance(value, str):
        return value
    if "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError("Текст содержит недопустимый символ.")
    return value.strip()


DatabaseText = Annotated[str, BeforeValidator(database_text)]
