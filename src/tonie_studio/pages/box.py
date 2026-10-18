"""What lies on the box, for every page's header to poll."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ..cache import TtlCache
from ..tags import list_tags
from ..teddycloud import TeddyCloudError
from .common import Context

# How long the name of the tonie on the box is reused, in seconds. A tonie
# renamed or given a new sticker shows its new name this much later in the header.
LABEL_TTL = 10.0


def register(app: FastAPI, ctx: Context) -> None:
    """Add the routes of this part of the app to ``app``."""
    box = ctx.box
    client = ctx.client

    # Every open page polls the box every few seconds; reading the whole tag
    # index for each poll would put teddyCloud under constant load for a name
    # that rarely changes.
    labels: TtlCache[str, str] = TtlCache(
        lambda ruid: next((tag.label for tag in list_tags(client()) if tag.ruid == ruid), ""),
        LABEL_TTL)

    def box_label(ruid: str) -> str:
        """The name of the tonie on the box, or nothing while teddyCloud is away."""
        try:
            return labels.get(ruid)
        except TeddyCloudError:
            return ""

    @app.get("/api/box")
    def box_state():
        """What is on the box, for the page to poll.

        Carries the tonie's name as well as its ruid: the header says what is
        playing, and looking that up per page would mean every page knowing how
        to read the tag index.
        """
        state = box.state
        return JSONResponse({
            "label": box_label(state.ruid) if state.known and state.on_box else "",
            "ruid": state.ruid if state.known else None,
            "on_box": state.on_box and state.known,
            "playing": state.playing and state.known,
            "since": state.since.isoformat() if state.since and state.known else None,
            "stale": state.stale,
            "connected": state.connected,
            # Why it is not: teddyCloud is up, but its event stream went quiet.
            "silent": state.silent and not state.connected,
            # A tag teddyCloud does not know: a fresh sticker, just held
            # against the box. The assignment page waits for this.
            "unknown_ruid": state.fresh_unknown,
            "unknown_since": state.unknown_since.isoformat() if state.unknown_since else None,
        })
