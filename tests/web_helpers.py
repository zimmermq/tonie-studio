"""Helpers the page tests share: getting an album id, running an encode, uploading a series."""

from contextlib import contextmanager
from unittest.mock import patch


def write_jpeg(path, size=(600, 600), colour=(10, 120, 200)):
    from PIL import Image

    # Marked in the corner, not flat: cropping and blurring both leave a single
    # colour untouched, so a flat cover would hide whether either happened.
    picture = Image.new("RGB", size, colour)
    picture.paste(Image.new("RGB", (size[0] // 6, size[1] // 6), (240, 200, 20)), (0, 0))
    picture.save(path, format="JPEG")
    return path


def first_album_id(client):
    """The id the picker gave the one album in the archive."""
    page = client.get("/").text
    start = page.index('name="ids" value="') + len('name="ids" value="')
    return page[start:page.index('"', start)]


def eventually(condition, timeout=5.0):
    """Wait for something a background thread does, without sleeping blindly."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition never became true")


def finish_job(runner, response):
    """Wait for the job a redirect points at and return it."""
    job_id = response.headers["location"].rsplit("/", 1)[1]
    job = runner.get(job_id)
    assert job.done.wait(5), "the encode did not finish"
    return job


def encode_and_assign_page(client, runner, **overrides):
    """Encode the archive album, then return its assignment page."""
    data = {
        "series": "Lichterkinder", "episode": "Halloween",
        "order": "1,2", "selected": ["1", "2"],
        "titles": ["Kleine Monster", "Suesses"],
    }
    data.update(overrides)
    response = client.post(f"/album/{first_album_id(client)}/encode", data=data,
                           follow_redirects=False)
    job = finish_job(runner, response)
    return job, client.get(f"/job/{job.id}/assign")


def publish_album(client, runner, teddycloud):
    """Encode and assign, so a custom model with a cover exists."""
    job, _page = encode_and_assign_page(client, runner)
    client.post(f"/job/{job.id}/assign", data={
        "ruid": "7ab4ed0e500304e0", "series": "Lichterkinder",
        "episode": "Halloween", "category": "music",
    }, follow_redirects=False)
    return next(iter(teddycloud.images))


def jpeg_bytes(size=(400, 400)):
    import io

    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buffer, format="JPEG")
    return buffer.getvalue()


@contextmanager
def captured_sheet():
    """Collect the stickers the route asks for, without rendering a PDF.

    The route answers with the file it just wrote, so the stand-in has to leave
    one behind or the response fails for a reason that has nothing to do with
    what is being tested.
    """
    asked = []

    def instead(stickers, target, shift=(0.0, 0.0), progress=None):
        asked.append(list(stickers))
        target.write_bytes(b"%PDF-1.4\n")
        return []

    with patch("tonie_studio.pages.printing.build_sheet", side_effect=instead):
        yield asked


def print_sheet(client, picks=(), files=None, **form):
    """Ask for a sheet as the print page does, wait for it, and answer what the download gives.

    ``picks`` are (model, count) or (model, count, mode), one per ticked row.
    A request refused before any sheet is started is answered as it is.
    """
    data = {"picks": [f"{pick[1]}:{pick[2] if len(pick) > 2 else 'fill'}:{pick[0]}"
                      for pick in picks], **form}
    response = client.post("/sheet", data=data, files=files, follow_redirects=False)
    if response.status_code != 303:
        return response
    job_id = response.headers["location"].rsplit("/", 1)[1]
    assert client.app.state.prints.get(job_id).done.wait(30)
    return client.get(f"/sheet/print/{job_id}/pdf")


def list_tags_of(teddycloud):
    from tonie_studio.tags import list_tags

    return list_tags(teddycloud)


def ruid_to_uid(ruid):
    """The reverse of ruid_from_uid, as the box would report the tag."""
    return "".join(ruid[index:index + 2] for index in range(14, -2, -2)).upper()


def album_page_after_upload(client, response):
    """The detailed page of the one album an upload made, reached the way a reader would."""
    batch_page = client.get(response.headers["location"]).text
    start = batch_page.index('href="/album/') + len('href="')
    return client.get(batch_page[start:batch_page.index('"', start)]).text


def upload_series(client, episodes=3):
    """Upload a parent folder holding one subfolder per episode, as a browser sends it."""
    files = []
    for number in range(episodes, 0, -1):
        files.append(("files", (f"Serie/Folge {number}/01-anfang.mp3", bytes([number]) * 8, "audio/mpeg")))
        files.append(("files", (f"Serie/Folge {number}/02-ende.mp3", bytes([number]) * 9, "audio/mpeg")))
    return client.post("/upload", files=files, follow_redirects=False)


def batch_ids(response):
    from urllib.parse import parse_qs, urlparse

    location = urlparse(response.headers["location"])
    assert location.path == "/batch"
    return parse_qs(location.query)["ids"]


def run_batch(client, runner, ids, series=None, **extra):
    data = {"ids": ids}
    if series is not None:
        data["series"] = [series] * len(ids)
    data.update(extra)
    response = client.post("/batch", data=data, follow_redirects=False)
    assert response.status_code == 303, response.text
    for job in runner.all():
        assert job.done.wait(5), "a batch encode did not finish"
    return response


def start_placing(client, runner, episodes=2):
    ids = batch_ids(upload_series(client, episodes=episodes))
    response = client.post("/batch", data={"ids": ids, "series": ["Serie"] * len(ids),
                                           "stickers": "place"}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return response.headers["location"].rsplit("/", 1)[1]


def place_sticker(client, group, ruid):
    response = client.post(f"/api/batch/{group}/place", data={"ruid": ruid})
    assert response.status_code == 200, response.text
    return response.json()


def current_step(page):
    marker = 'aria-current="step" data-step="'
    start = page.index(marker) + len(marker)
    return page[start:page.index('"', start)]


def start_placing_archive(client, identifier):
    response = client.post("/batch", data={"ids": [identifier], "series": ["L"],
                                           "stickers": "place"}, follow_redirects=False)
    return response.headers["location"].rsplit("/", 1)[1]


def group_jobs(page):
    import re

    return re.findall(r'data-job="([0-9a-f]+)"', page)


def checked_ruid(page):
    import re

    for match in re.finditer(r'<input type="radio" name="ruid" value="([0-9a-f]+)"[^>]*>', page, re.S):
        if "checked" in match.group(0):
            return match.group(1)
    return None


def page_script(page, name):
    """The address a page loads its own script ``name`` from."""
    start = page.index(f'src="/static/{name}?v=') + len('src="')
    return page[start:page.index('"', start)]
