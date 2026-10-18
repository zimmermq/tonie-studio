"""Tests for pictures far larger than any cover: refused like an unreadable one."""


from PIL import Image


def huge_png(pixels):
    """A PNG whose header claims ``pixels`` pixels; tiny on disk, huge once decoded."""
    import struct
    import zlib

    side = int(pixels ** 0.5)

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b""))


class TestPixelBombs:
    def test_the_limit_is_set_low_enough(self):
        import tonie_studio.pictures  # noqa: F401 - setting the limit is its import

        assert Image.MAX_IMAGE_PIXELS <= 50_000_000

    def test_a_cover_that_is_one_is_served_as_it_came(self, client, teddycloud):
        # Not a 500 for every thumbnail of the list.
        teddycloud.images["TS-0001.png"] = huge_png(120_000_000)

        response = client.get("/cover/custom/TS-0001.png?size=96")

        assert response.status_code == 200

    def test_a_sheet_leaves_it_off_instead_of_failing(self, tmp_path):
        from tonie_studio.sheet import Sticker, build_sheet
        from web_helpers import jpeg_bytes

        skipped = build_sheet([Sticker(image=huge_png(120_000_000), label="Riesig"),
                               Sticker(image=jpeg_bytes(), label="Normal")],
                              tmp_path / "sheet.pdf", (0.0, 0.0))

        assert skipped == ["Riesig"] and (tmp_path / "sheet.pdf").exists()
