from asyncio import AbstractEventLoop
from typing import Any, Optional
from patchright._impl._api_structures import WebErrorLocation
from patchright._impl._helper import Error
from patchright._impl._page import Page


class WebError:
    def __init__(
        self,
        loop: AbstractEventLoop,
        dispatcher_fiber: Any,
        page: Optional[Page],
        error: Error,
        location: WebErrorLocation,
    ) -> None:
        self._loop = loop
        self._dispatcher_fiber = dispatcher_fiber
        self._page = page
        self._error = error
        self._location = location

    @property
    def page(self) -> Optional[Page]:
        return self._page

    @property
    def error(self) -> Error:
        return self._error

    @property
    def location(self) -> WebErrorLocation:
        return self._location
