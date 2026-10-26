"""What the box has played, and the podcasts that keep a sticker up to date."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import requests
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..listening import HEARD, QUIET, heard
from ..listing import Column, filters
from ..podcast import (DEFAULT_KEEP, LARGEST_FEED, FeedError, load_subscriptions, parse_feed,
                       remove_subscription)
from ..podcast import REFRESH_AFTER as ARCHIVE_REFRESH_AFTER
from ..podcast import subscribe as add_podcast
from ..podcast_archive import (MOST_TOGETHER, add_archive, load_archives, put_together,
                               read_source, remove_archive)
from ..podcast_search import search as search_podcasts
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
    def podcasts_page(request: Request, ruid: str = "", query: str = "", feed: str = ""):
        """``ruid`` chooses that sticker, for a page that sent someone here with one.

        ``query`` searches the ARD Audiothek and the podcast directory; ``feed``
        fills in the subscription form, as a result found there does.
        """
        tags = list_tags(client())
        labels = {tag.ruid: tag.label for tag in tags}
        found, failed = search_podcasts(query, fetch) if query.strip() else ([], [])
        stored = load_archives(settings.data_dir)
        return templates.TemplateResponse(request, "podcasts.html", {
            "query": query,
            "found": found,
            "failed": failed,
            "archived_sources": {entry.source for entry in stored},
            "feed": feed,
            "subscriptions": load_subscriptions(settings.data_dir),
            "labels": labels,
            "tags": tags,
            "choose": ruid if ruid in labels else None,
            "keep": DEFAULT_KEEP,
            "archives": [(entry, _round_of(entry), _minutes_left(entry), _missing(entry))
                         for entry in stored],
        })

    def _round_of(entry):
        """The job storing for the archive now, or None."""
        job_id = archives.running(entry.id)
        return ctx.jobs.get(job_id) if job_id else None


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
            feed = read_source(source, fetch, glance=True)
        except (FeedError, requests.RequestException) as error:
            raise Refused(400, "error.feed_unreadable", detail=error) from error
        if not feed.episodes:
            raise Refused(400, "feed.no_audio")
        archive = add_archive(settings.data_dir, source, feed.title)
        job_id = archives.queue(archive.id)
        return RedirectResponse(f"/job/{job_id}" if job_id else "/podcasts", 303)


    @app.post("/podcasts/archives/{archive_id}/refresh")
    def refresh_archive_now(archive_id: str):
        """Start a round, or lead to the one already running."""
        job_id = archives.queue(archive_id) or archives.running(archive_id)
        return RedirectResponse(f"/job/{job_id}" if job_id else "/podcasts", 303)


    def archive_or_404(archive_id: str):
        found = next((entry for entry in load_archives(settings.data_dir)
                      if entry.id == archive_id), None)
        if found is None:
            raise Refused(404, "archive.gone")
        return found


    @app.get("/podcasts/archives/{archive_id}/together", response_class=HTMLResponse)
    def together_page(request: Request, archive_id: str):
        """Choose archived episodes to put onto one sticker, newest first."""
        archive = archive_or_404(archive_id)
        episodes = sorted(archive.archived.items(), key=lambda item: item[1].published,
                          reverse=True)
        return templates.TemplateResponse(request, "podcast_together.html", {
            "archive": archive,
            "episodes": episodes,
            "tags": list_tags(client()),
            "most": MOST_TOGETHER,
        })


    @app.post("/podcasts/archives/{archive_id}/together")
    def together(archive_id: str, ruid: str = Form(...), name: str = Form(""),
                 episodes: List[str] = Form(default=[])):
        """Check what can be checked now, then encode and publish as a job."""
        archive = archive_or_404(archive_id)
        chosen = [guid for guid in dict.fromkeys(episodes) if guid in archive.archived]
        if not chosen:
            raise Refused(400, "archive.choose_episodes")
        if len(chosen) > MOST_TOGETHER:
            raise Refused(400, "archive.too_many", most=MOST_TOGETHER)
        if not name.strip():
            raise Refused(400, "encode.no_name")
        if find_tag(client(), ruid) is None:
            raise Refused(404, "error.no_tag")
        job_id = ctx.jobs.submit(
            "together", name.strip(),
            lambda progress: put_together(client(), archive, chosen, name.strip(), ruid,
                                          settings.data_dir, progress))
        return RedirectResponse(f"/job/{job_id}", 303)


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


# Fewer episodes timed than this say too little about the rest to estimate it.
TIMED_ENOUGH = 3


def _minutes_left(entry) -> Optional[Tuple[int, int]]:
    """How many episodes are left and about how many minutes they take; None when unknown."""
    left = max(entry.total - len(entry.archived) - len(entry.missing), 0)
    if not left or entry.timed < TIMED_ENOUGH:
        return None
    return left, max(1, round(left * entry.spent / entry.timed / 60))


def _missing(entry) -> List[Tuple[str, Optional[datetime]]]:
    """Each episode the source no longer has, with when it is asked for again."""
    found = []
    for gone in entry.missing.values():
        try:
            again = datetime.fromisoformat(gone.checked) + ARCHIVE_REFRESH_AFTER
        except ValueError:
            again = None
        found.append((gone.title, again))
    return found
