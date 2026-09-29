"""Tests for the cover previews on the album picker."""

import io

import pytest
from PIL import Image

from tonie_studio import thumbnails
from tonie_studio.thumbnails import DEFAULT_SIZE, MAX_SIZE, thumbnail


@pytest.fixture(autouse=True)
def empty_cache():
    thumbnails._cache.clear()
    yield
    thumbnails._cache.clear()


def _cover(path, size=(800, 800), colour=(200, 60, 40)):
    Image.new("RGB", size, colour).save(path, format="JPEG")
    return path


def _size_of(data):
    with Image.open(io.BytesIO(data)) as image:
        return image.size


class TestThumbnail:
    def test_renders_a_square_of_the_requested_size(self, tmp_path):
        data = thumbnail(_cover(tmp_path / "cover.jpg"), size=64)

        assert _size_of(data) == (64, 64)

    def test_crops_a_wide_cover_instead_of_squashing_it(self, tmp_path):
        # A row of covers only lines up if every preview is square, and a
        # squashed cover is worse than a cropped one.
        data = thumbnail(_cover(tmp_path / "wide.jpg", size=(1200, 400)), size=64)

        assert _size_of(data) == (64, 64)

    def test_defaults_to_a_size_that_stays_sharp(self, tmp_path):
        data = thumbnail(_cover(tmp_path / "cover.jpg"))

        assert _size_of(data) == (DEFAULT_SIZE, DEFAULT_SIZE)

    def test_is_much_smaller_than_the_original(self, tmp_path):
        path = _cover(tmp_path / "cover.jpg", size=(1500, 1500))

        assert len(thumbnail(path, size=64)) < path.stat().st_size / 4

    def test_refuses_to_render_something_enormous(self, tmp_path):
        data = thumbnail(_cover(tmp_path / "cover.jpg"), size=99999)

        assert _size_of(data) == (MAX_SIZE, MAX_SIZE)


class TestFailures:
    def test_answers_none_for_a_file_that_is_not_an_image(self, tmp_path):
        broken = tmp_path / "notes.jpg"
        broken.write_bytes(b"not an image")

        assert thumbnail(broken) is None

    def test_answers_none_for_a_file_that_is_not_there(self, tmp_path):
        assert thumbnail(tmp_path / "gone.jpg") is None


class TestCache:
    def test_renders_a_cover_only_once(self, tmp_path):
        path = _cover(tmp_path / "cover.jpg")
        calls = []
        original = thumbnails._render

        def counted(*args):
            calls.append(args)
            return original(*args)

        thumbnails._render = counted
        try:
            thumbnail(path, size=64)
            thumbnail(path, size=64)
        finally:
            thumbnails._render = original

        assert len(calls) == 1

    def test_notices_a_cover_that_was_replaced(self, tmp_path):
        # The key carries the modification time, so swapping the file out is
        # enough; nobody has to clear anything.
        path = _cover(tmp_path / "cover.jpg", colour=(10, 10, 10))
        first = thumbnail(path, size=32)

        import os

        _cover(path, colour=(240, 240, 240))
        os.utime(path, (1, 1))

        assert thumbnail(path, size=32) != first

    def test_keeps_different_sizes_apart(self, tmp_path):
        path = _cover(tmp_path / "cover.jpg")

        assert _size_of(thumbnail(path, size=32)) == (32, 32)
        assert _size_of(thumbnail(path, size=64)) == (64, 64)

    def test_does_not_grow_without_bound(self, tmp_path):
        for index in range(thumbnails.CACHE_ENTRIES + 20):
            thumbnail(_cover(tmp_path / f"c{index}.jpg", size=(64, 64)), size=32)

        assert len(thumbnails._cache) <= thumbnails.CACHE_ENTRIES


class TestCrop:
    def test_renders_the_whole_cover_at_zoom_one(self, tmp_path):
        from tonie_studio.thumbnails import COVER_SIZE, crop

        data = crop(_cover(tmp_path / "cover.jpg"), size=64)

        assert _size_of(data) == (64, 64)
        assert crop(_cover(tmp_path / "c2.jpg")) is not None
        assert _size_of(crop(_cover(tmp_path / "c3.jpg"))) == (COVER_SIZE, COVER_SIZE)

    def test_zooming_in_shows_less_of_the_picture(self, tmp_path):
        # A picture with a distinct centre: zoomed in, the edges must be gone.
        from PIL import Image

        from tonie_studio.thumbnails import crop

        image = Image.new("RGB", (300, 300), (255, 0, 0))
        image.paste(Image.new("RGB", (100, 100), (0, 0, 255)), (100, 100))
        path = tmp_path / "centre.jpg"
        image.save(path, format="JPEG")

        wide = crop(path, zoom=1.0, size=32)
        close = crop(path, zoom=3.0, size=32)

        assert _blue_share(close) > _blue_share(wide)

    def test_shifting_moves_what_the_circle_keeps(self, tmp_path):
        from PIL import Image

        from tonie_studio.thumbnails import crop

        image = Image.new("RGB", (300, 300), (255, 0, 0))
        image.paste(Image.new("RGB", (100, 300), (0, 0, 255)), (0, 0))
        path = tmp_path / "left.jpg"
        image.save(path, format="JPEG")

        left = crop(path, zoom=2.0, offset_x=-1.0, size=32)
        right = crop(path, zoom=2.0, offset_x=1.0, size=32)

        assert _blue_share(left) > _blue_share(right)

    def test_zooming_out_keeps_the_corners_the_circle_would_cut(self, tmp_path):
        # The lettering on a cover sits at the edges, which the circle eats.
        # Zoomed out, a mark in the very corner has to survive.
        from PIL import Image

        from tonie_studio.thumbnails import crop

        image = Image.new("RGB", (300, 300), (255, 0, 0))
        image.paste(Image.new("RGB", (40, 40), (0, 0, 255)), (0, 0))
        path = tmp_path / "corner.jpg"
        image.save(path, format="JPEG")

        assert _blue_share(crop(path, zoom=0.6, size=64)) > \
               _blue_share(crop(path, zoom=1.0, size=64))

    def test_zooming_out_fills_the_gap_instead_of_leaving_an_edge(self, tmp_path):
        # A flat border reads as a rendering mistake, so the space around the
        # shrunken cover carries the cover's own colours.
        from PIL import Image

        from tonie_studio.thumbnails import crop

        data = crop(_cover(tmp_path / "cover.jpg", colour=(200, 60, 40)), zoom=0.6, size=64)
        picture = Image.open(io.BytesIO(data))

        assert picture.getpixel((2, 2))[0] > 100  # the corner is cover, not paper

    def test_refuses_to_zoom_out_past_what_is_sensible(self, tmp_path):
        from tonie_studio.thumbnails import MIN_ZOOM, crop

        # Smaller than this the cover is a stamp on a blurred disc.
        assert crop(_cover(tmp_path / "cover.jpg"), zoom=0.01, size=32) == \
               crop(_cover(tmp_path / "cover.jpg"), zoom=MIN_ZOOM, size=32)

    def test_refuses_to_zoom_past_what_is_sensible(self, tmp_path):
        from tonie_studio.thumbnails import crop

        # Beyond 3x a 600px cover is mush; clamping beats a pixelated sticker.
        assert crop(_cover(tmp_path / "cover.jpg"), zoom=99, size=32) == \
               crop(_cover(tmp_path / "cover.jpg"), zoom=3, size=32)

    def test_a_shift_cannot_walk_out_of_the_picture(self, tmp_path):
        from tonie_studio.thumbnails import crop

        assert crop(_cover(tmp_path / "cover.jpg"), zoom=1.5, offset_x=99, size=32) is not None

    def test_answers_none_for_something_it_cannot_read(self, tmp_path):
        from tonie_studio.thumbnails import crop

        broken = tmp_path / "broken.jpg"
        broken.write_bytes(b"nope")

        assert crop(broken) is None


def _blue_share(data):
    """How much of the rendered square is the blue patch."""
    with Image.open(io.BytesIO(data)) as image:
        picture = image.convert("RGB")
        pixels = [picture.getpixel((x, y))
                  for y in range(picture.height) for x in range(picture.width)]
    return sum(1 for r, g, b in pixels if b > r) / len(pixels)
