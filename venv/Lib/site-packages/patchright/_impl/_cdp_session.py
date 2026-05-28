from types import SimpleNamespace
from typing import Any, Dict
from patchright._impl._connection import ChannelOwner
from patchright._impl._helper import locals_to_params


class CDPSession(ChannelOwner):
    Events = SimpleNamespace(Event="event", Close="close")

    def __init__(
        self, parent: ChannelOwner, type: str, guid: str, initializer: Dict
    ) -> None:
        super().__init__(parent, type, guid, initializer)
        self._channel.on("event", lambda params: self._on_event(params))
        self._channel.on("close", lambda _: self.emit(CDPSession.Events.Close, self))

    def _on_event(self, params: Any) -> None:
        self.emit(params["method"], params.get("params"))
        self.emit(CDPSession.Events.Event, params)

    async def send(self, method: str, params: Dict = None) -> Dict:
        return await self._channel.send("send", None, locals_to_params(locals()))

    async def detach(self) -> None:
        await self._channel.send("detach", None)
