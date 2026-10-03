"""An in-memory stand-in for teddyCloud, with the rules it enforces kept in.

Asserting on mock calls would prove the client was asked to do something, not
that the something was possible. This fake refuses what the real server refuses
- writing into a directory that does not exist, encoding onto an existing taf,
deleting a directory that still has contents - so a test fails where teddyCloud
would have failed.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set

from tonie_studio.teddycloud import TeddyCloudError


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] or "/"


def _name(path: str) -> str:
    return path.rsplit("/", 1)[1]


def _join(directory: str, name: str) -> str:
    return f"{directory.rstrip('/')}/{name}"


class FakeTeddyCloud:
    """Enough of the library tree to run an encode against."""

    def __init__(self) -> None:
        self.dirs: Set[str] = {"/"}
        self.files: Dict[str, bytes] = {}
        self.taf_headers: Dict[str, dict] = {}
        # Every encode that ran, as (target, sources), for order assertions.
        self.encoded: List[tuple] = []
        # Set to raise from the next encode, to test the failure path.
        self.encode_fails: Optional[str] = None
        # The custom_img tree, which teddyCloud keeps apart from the library.
        self.images: Dict[str, bytes] = {}
        # Tag entries as content/<CN>/<UID>.json holds them, keyed by ruid.
        self.tag_entries: Dict[str, dict] = {}
        self.custom_models: List[dict] = []

    def add_tag(self, ruid: str, **fields) -> None:
        """Pretend the box has read this tag, which is the only way one appears."""
        entry = {"source": "", "tonie_model": "", "live": False, "nocloud": False,
                 "hide": False, "claimed": False}
        entry.update(fields)
        self.tag_entries[ruid] = entry

    def list_dir(self, path: str = "/", special: Optional[str] = None) -> List[dict]:
        if special == "custom_img":
            return [{"name": "..", "isDir": True}] + [
                {"name": name, "size": len(data), "isDir": False}
                for name, data in sorted(self.images.items())
            ]
        if path not in self.dirs:
            raise TeddyCloudError(f"no such directory: {path}")

        # teddyCloud lists the link to the parent as an ordinary entry.
        entries = [{"name": "..", "date": 0, "size": 4096, "isDir": True}]
        for directory in sorted(self.dirs):
            if directory != "/" and _parent(directory) == path:
                entries.append({"name": _name(directory), "date": 0, "size": 0, "isDir": True})
        for file in sorted(self.files):
            if _parent(file) == path:
                entry = {
                    "name": _name(file),
                    "date": 0,
                    "size": len(self.files[file]),
                    "isDir": False,
                }
                if file in self.taf_headers:
                    entry["tafHeader"] = self.taf_headers[file]
                entries.append(entry)
        return entries

    def create_dir(self, path: str, special: Optional[str] = None) -> None:
        if path in self.dirs:
            raise TeddyCloudError(f"directory exists: {path}")
        if _parent(path) not in self.dirs:
            raise TeddyCloudError(f"no such directory: {_parent(path)}")
        self.dirs.add(path)

    def upload(self, stream, filename: str, path: str = "/", special: Optional[str] = None) -> None:
        if "/" in filename or "\\" in filename:
            raise ValueError(f"upload name must not contain a path separator: {filename!r}")
        if special == "custom_img":
            self.images[filename] = stream.read()
            return
        if path not in self.dirs:
            raise TeddyCloudError(f"no such directory: {path}")
        self.files[_join(path, filename)] = stream.read()

    def encode(self, target: str, sources, special: str = "library") -> None:
        sources = list(sources)
        if self.encode_fails:
            raise TeddyCloudError(self.encode_fails)
        if not sources:
            raise ValueError("an encode needs at least one source file")
        if target in self.files:
            raise TeddyCloudError(f"target exists: {target}")
        if _parent(target) not in self.dirs:
            raise TeddyCloudError(f"no such directory: {_parent(target)}")
        for source in sources:
            if source not in self.files:
                raise TeddyCloudError(f"no such file: {source}")

        self.encoded.append((target, sources))
        self.files[target] = b"".join(self.files[source] for source in sources)
        self.taf_headers[target] = {
            "audioId": 445848115,
            "sha1Hash": "0d7e5ac7de7b3cdfa865f97081225e26cf6c39d2",
            "size": len(self.files[target]),
            "valid": True,
            "trackSeconds": [0] * len(sources),
        }

    def fetch(self, path: str) -> bytes:
        if path.startswith("/custom_img/"):
            name = path[len("/custom_img/"):]
            if name not in self.images:
                raise TeddyCloudError(f"no such image: {path}")
            return self.images[name]
        if path in self.files:
            return self.files[path]
        raise TeddyCloudError(f"no such file: {path}")

    def tags(self) -> List[dict]:
        """The tag index, shaped the way getTagIndex answers."""
        index = [{
            "ruid": "0000000000000000", "uid": "00:00:00:00:00:00:00:00", "type": "system",
            "valid": False, "source": "", "hide": False, "claimed": False,
            "tonieInfo": {"model": "", "series": "", "episode": "",
                          "picture": "/img_unknown.png", "tracks": []},
        }]
        for ruid, entry in sorted(self.tag_entries.items()):
            model = next((m for m in self.custom_models if m["model"] == entry["tonie_model"]), {})
            index.append({
                "ruid": ruid,
                "uid": ":".join(ruid[index_:index_ + 2] for index_ in range(14, -2, -2)).upper(),
                "type": "tag",
                "valid": bool(entry["source"]),
                "source": entry["source"],
                "live": entry["live"],
                "nocloud": entry["nocloud"],
                "hide": entry["hide"],
                "claimed": entry["claimed"],
                "tonieInfo": {
                    "model": entry["tonie_model"],
                    "series": model.get("series", ""),
                    "episode": model.get("episodes", ""),
                    "picture": model.get("pic", "/img_unknown.png"),
                    "tracks": model.get("tracks", []),
                },
            })
        return index

    def set_tag(self, ruid: str, **fields) -> None:
        if ruid not in self.tag_entries:
            raise TeddyCloudError(f"no such tag: {ruid}")
        if not fields:
            raise ValueError("set_tag needs at least one field to change")
        self.tag_entries[ruid].update(fields)

    def custom_tonies(self) -> List[dict]:
        return [dict(model) for model in self.custom_models]

    def upsert_custom_tonie(self, model: dict) -> None:
        missing = [field for field in ("model", "series") if not model.get(field)]
        if missing:
            raise ValueError(f"custom model needs {' and '.join(missing)}")
        for index, existing in enumerate(self.custom_models):
            if existing["model"].lower() == model["model"].lower():
                self.custom_models[index] = dict(model)
                return
        self.custom_models.append(dict(model))

    def delete_custom_tonies(self, models) -> None:
        wanted = {model.lower() for model in models}
        self.custom_models = [m for m in self.custom_models if m["model"].lower() not in wanted]

    def delete(self, path: str, special: Optional[str] = None) -> None:
        if special == "custom_img":
            self.images.pop(path.lstrip("/"), None)
            return
        if path in self.files:
            del self.files[path]
            self.taf_headers.pop(path, None)
            return
        if path in self.dirs:
            prefix = f"{path}/"
            if any(item.startswith(prefix) for item in list(self.files) + list(self.dirs)):
                raise TeddyCloudError(f"directory not empty: {path}")
            self.dirs.discard(path)
            return
        raise TeddyCloudError(f"no such file: {path}")
