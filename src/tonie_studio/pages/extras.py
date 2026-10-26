"""What the box has played, and the podcasts that keep a sticker up to date."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..listening import HEARD, QUIET, heard
from ..listing import Column, filters
from ..podcast import (DEFAULT_KEEP, LARGEST_FEED, FeedError, load_subscriptions, parse_feed,
                       remove_subscription)
from ..podcast import subscribe as add_podcast
from ..podcast_archive import add_archive, load_archives, read_source, remove_archive
from ..runtimes import playing_times
from ..tags import find_tag, list_tags
from .common import Context, Refused

log = logging.getLogger(__name__)

# The periods the listening page offers, by the value in its address.
LISTENING_PERIODS = {"7": 7, "30": 30, "all": None}


def register(app: FastAPI, ctx: Context) -> None:
    """Add the routes of this part of the app to ``app``."""
    settings = ctx.settings
    templates = ctx.templates
    box = ctx.box
    listening = ctx.listening
    client = ctx.client
    fetch = ctx.fetch
    podcasts = ctx.podcasts
    archives = ctx.archives

    @app.get("/listening", response_class=HTMLResponse)
    def listening_page(request: Request, period: str = "30"):
        """Which tonies were heard, how much, and which have gone quiet.

        Only tags that still play something are listed: a sticker that was
        emptied has nothing to recommend or to miss.
        """
        if period not in LISTENING_PERIODS:
            period = "30"
        days = LISTENING_PERIODS[period]
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=days) if days else None

        # The last bundle from the box is held back until it is complete;
        # take it now, so what was just heard is on the page.
        box.flush()
        events = listening.events()
        rows = heard(events, list_tags(client()), playing_times(settings.data_dir), now, since)
        counts, offered = filters(([row.kind] for row in rows), (HEARD, QUIET))
        return templates.TemplateResponse(request, "listening.html", {
            "rows": rows,
            "counts": counts,
            "filters": offered,
            "columns": LISTENING_COLUMNS,
            "period": period,
            "days": days,
            "recorded_since": events[0].at if events else None,
        })


    @app.get("/podcasts", response_class=HTMLResponse)
    def podcasts_page(request: Request, ruid: str = ""):
        """``ruid`` chooses that sticker, for a page that sent someone here with one."""
        tags = list_tags(client())
        labels = {tag.ruid: tag.label for tag in tags}
        return templates.TemplateResponse(request, "podcasts.html", {
            "subscriptions": load_subscriptions(settings.data_dir),
            "labels": labels,
            "tags": tags,
            "choose": ruid if ruid in labels else None,
            "keep": DEFAULT_KEEP,
            "archives": load_archives(settings.data_dir),
        })


    @app.post("/podcasts")
    def subscribe(feed: str = Form(...), ruid: str = Form(...), keep: int = Form(DEFAULT_KEEP)):
        """Check the feed now, so a wrong address is said on the form, then start."""
        feed = feed.strip()
        try:
            parsed = parse_feed(fetch(feed, LARGEST_FEED))
        except (FeedError, requests.RequestException) as error:
            raise Refused(400, "error.feed_unreadable", detail=error) from error
        if not parsed.episodes:
            raise Refused(400, "feed.no_audio")
        tag = find_tag(client(), ruid)
        if tag is None:
            raise Refused(404, "error.no_tag")

        subscription = add_podcast(settings.data_dir, feed, ruid, keep, title=parsed.title,
                                   played_before=tag.source or "")
        job_id = podcasts.queue(subscription.id)
        return RedirectResponse(f"/job/{job_id}" if job_id else "/podcasts", 303)


    @app.post("/podcasts/{subscription_id}/refresh")
    def refresh_now(subscription_id: str):
        job_id = podcasts.queue(subscription_id)
        return RedirectResponse(f"/job/{job_id}" if job_id else "/podcasts", 303)


    @app.post("/podcasts/{subscription_id}/delete")
    def unsubscribe(subscription_id: str):
        """Stop updating. The sticker keeps playing the episodes it has."""
        remove_subscription(settings.data_dir, subscription_id)
        return RedirectResponse("/podcasts", 303)


    @app.post("/podcasts/archives")
    def start_archive(source: str = Form(...)):
        """Check the source now, so a wrong address is said on the form, then start."""
        source = source.strip()
        if any(entry.source == source for entry in load_archives(settings.data_dir)):
            raise Refused(409, "archive.exists")
        try:
            feed = read_source(source, fetch)
        except (FeedError, requests.RequestException) as error:
            raise Refused(400, "error.feed_unreadable", detail=error) from error
        if not feed.episodes:
            raise Refused(400, "feed.no_audio")
        archive = add_archive(settings.data_dir, source, feed.title)
        job_id = archives.queue(archive.id)
        return RedirectResponse(f"/job/{job_id}" if job_id else "/podcasts", 303)


    @app.post("/podcasts/archives/{archive_id}/refresh")
    def refresh_archive_now(archive_id: str):
        job_id = archives.queue(archive_id)
        return RedirectResponse(f"/job/{job_id}" if job_id else "/podcasts", 303)


    @app.post("/podcasts/archives/{archive_id}/delete")
    def stop_archive(archive_id: str):
        """Stop archiving. The episodes stay in the library."""
        remove_archive(settings.data_dir, archive_id)
        return RedirectResponse("/podcasts", 303)


# The listening list's columns, in the order the rows show them. It starts
# with what was played most; ordered by when a tonie was last placed, one
# never placed comes first, as the one forgotten longest.
LISTENING_COLUMNS = (
    Column("name", "col.name"),
    Column("played", "listening.col_played", number=True, desc=True),
    Column("placed", "listening.col_placed", number=True, desc=True),
    Column("last", "listening.col_last", number=True),
)
