import pathlib
from typing import List, Tuple, Union
from patchright._impl._api_structures import FilePayload

FormDataValue = Union[bool, float, str, pathlib.Path, FilePayload]


class FormData:
    def __init__(self) -> None:
        self._fields: List[Tuple[str, FormDataValue]] = []

    def set(self, name: str, value: FormDataValue) -> "FormData":
        self._fields = [(n, v) for n, v in self._fields if n != name]
        self._fields.append((name, value))
        return self

    def append(self, name: str, value: FormDataValue) -> "FormData":
        self._fields.append((name, value))
        return self
