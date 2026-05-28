from types import SimpleNamespace
from typing import Any, Dict, Optional
from patchright._impl._api_structures import DebuggerLocation, DebuggerPausedDetails
from patchright._impl._connection import ChannelOwner


class Debugger(ChannelOwner):
    Events = SimpleNamespace(PausedStateChanged="pausedstatechanged")

    def __init__(
        self, parent: ChannelOwner, type: str, guid: str, initializer: Dict
    ) -> None:
        super().__init__(parent, type, guid, initializer)
        self._paused_details: Optional[DebuggerPausedDetails] = None
        self._channel.on(
            "pausedStateChanged", lambda params: self._on_paused_state_changed(params)
        )

    def _on_paused_state_changed(self, params: Dict[str, Any]) -> None:
        self._paused_details = params.get("pausedDetails")
        self.emit(Debugger.Events.PausedStateChanged)

    async def request_pause(self) -> None:
        await self._channel.send("requestPause", None)

    async def resume(self) -> None:
        await self._channel.send("resume", None)

    async def next(self) -> None:
        await self._channel.send("next", None)

    async def run_to(self, location: DebuggerLocation) -> None:
        await self._channel.send("runTo", None, {"location": location})

    @property
    def paused_details(self) -> Optional[DebuggerPausedDetails]:
        return self._paused_details
