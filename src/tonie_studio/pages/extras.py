"""What the box has played, and the podcasts that keep a sticker up to date."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import requests
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..listening import summarise
from ..podcast import (DEFAULT_KEEP, LARGEST_FEED, FeedError, Subscription, add_subscription,
                       load_subscriptions, parse_feed, remove_subscription)
from ..runtimes import playing_times
from ..tonie import list_tags
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
    queue_podcast = ctx.queue_podcast

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
        tags = list_tags(client())
        # How long each tonie plays caps what one run of it can be credited.
        times = playing_times(settings.data_dir)
        content = {tag.ruid: timedelta(seconds=times[tag.model])
                   for tag in tags if tag.model in times}
        summary = summarise(events, now=now, since=since, content=content)
        heard, quiet = [], []
        for tag in tags:
            if not tag.assigned:
                continue
            entry = summary.get(tag.ruid)
            if entry is not None and (entry.placed or entry.on_box):
                heard.append((tag, entry))
            else:
                quiet.append((tag, entry))

        heard.sort(key=lambda pair: (pair[1].played, pair[1].on_box, pair[1].placed),
                   reverse=True)
        # Never heard first, then the longest silence: the top of the list is
        # what has been forgotten longest.
        quiet.sort(key=lambda pair: (pair[1] is not None and pair[1].last_placed is not None,
                                     pair[1].last_placed if pair[1] and pair[1].last_placed
                                     else now))
        return templates.TemplateResponse(request, "listening.html", {
            "heard": heard,
            "quiet": quiet,
            "period": period,
            "days": days,
            "recorded_since": events[0].at if events else None,
        })


    @app.get("/podcasts", response_class=HTMLResponse)
    def podcasts_page(request: Request):
        tags = list_tags(client())
        labels = {tag.ruid: tag.label for tag in tags}
        return templates.TemplateResponse(request, "podcasts.html", {
            "subscriptions": load_subscriptions(settings.data_dir),
            "labels": labels,
            "tags": tags,
            "keep": DEFAULT_KEEP,
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
        if not any(tag.ruid == ruid for tag in list_tags(client())):
            raise Refused(400, "error.no_tag")

        subscription = Subscription(id=uuid.uuid4().hex[:10], feed=feed, ruid=ruid,
                                    keep=max(1, min(keep, 20)), title=parsed.title)
        add_subscription(settings.data_dir, subscription)
        job_id = queue_podcast(subscription.id)
        return RedirectResponse(f"/job/{job_id}" if job_id else "/podcasts", 303)


    @app.post("/podcasts/{subscription_id}/refresh")
    def refresh_now(subscription_id: str):
        job_id = queue_podcast(subscription_id)
        return RedirectResponse(f"/job/{job_id}" if job_id else "/podcasts", 303)


    @app.post("/podcasts/{subscription_id}/delete")
    def unsubscribe(subscription_id: str):
        """Stop updating. The sticker keeps playing the episodes it has."""
        remove_subscription(settings.data_dir, subscription_id)
        return RedirectResponse("/podcasts", 303)
