"""An in-memory stand-in for teddyCloud, with the rules it enforces kept in.

Asserting on mock calls would prove the client was asked to do something, not
that the something was possible. This fake refuses what the real server refuses
- writing into a directory that does not exist, encoding onto an existing taf,
deleting a directory that still has contents - so a test fails where teddyCloud
would have failed.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional, Set

from tonie_studio.teddycloud import TeddyCloudError


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] or "/"


def _name(path: str) -> str:
    return path.rsplit("/", 1)[1]


def _join(directory: str, name: str) -> str:
    return f"{directory.rstrip('/')}/{name}"


class FakeTeddyCloud:
    """Enough of the library tree to run an encode against.

    The library and the content tree (``special`` left out) share one tree
    here, because the tests set up library files without naming the tree.
    Content folders are eight upper-case hex digits and never collide with
    the library's series folders in a test.
    """

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
        self.settings: Dict[str, str] = {
            "cloud.enabled": "true", "cloud.cacheContent": "true", "cloud.cacheToLibrary": "true",
            "tonie_json.cache_images": "true",
        }
        # What the Tonies cloud would hand out, by ruid, and every ruid asked for.
        self.cloud: Dict[str, bytes] = {}
        self.cloud_requests: List[str] = []
        # Lookups of a tonie whose content folder does not exist, which the
        # real server logs as a 404 warning each.
        self.content_misses = 0
        # The official catalogue, tonies.json.
        self.catalogue: List[dict] = []
        # teddyCloud 0.7.0 refuses a custom model whose audio_id and hash another
        # custom model already carries. Off by default: the encode above gives
        # every taf the same audio, which real encodes never do.
        self.unique_audio = False

    def twin(self) -> "FakeTeddyCloud":
        # One process, no sessions: every thread may share the fake.
        return self

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
                "valid": bool(entry["source"]) or bool(entry.get("exists")),
                # Whether the audio it would play is there, in the library or
                # cached beside its content JSON.
                "exists": bool(entry["source"]) or bool(entry.get("exists")),
                "source": entry["source"],
                "live": entry["live"],
                "nocloud": entry["nocloud"],
                "hide": entry["hide"],
                "claimed": entry["claimed"],
                "tonieInfo": {
                    "model": entry["tonie_model"],
                    "series": model.get("series", ""),
                    "episode": model.get("episodes", ""),
                    "picture": entry.get("picture") or model.get("pic", "/img_unknown.png"),
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
        # teddyCloud refuses two custom models with the same audio_id and hash.
        pairs = {(str(a), str(h).upper()) for a in model.get("audio_id") or []
                 for h in model.get("hash") or []} if self.unique_audio else set()
        for other in self.custom_models:
            if other["model"].lower() == model["model"].lower():
                continue
            if pairs & {(str(a), str(h).upper()) for a in other.get("audio_id") or []
                        for h in other.get("hash") or []}:
                raise TeddyCloudError("teddyCloud answered 400 for /api/toniesCustomJsonUpsert: "
                                      "Duplicate audio_id+hash pair detected", status=400)
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

    # --- content and the Tonies cloud ---------------------------------------

    def add_content(self, ruid: str, **fields) -> None:
        """Pretend a content JSON exists for ``ruid``, written by the box or an import."""
        folder = f"/{ruid[:8].upper()}"
        self.dirs.add(folder)
        self.files[f"{folder}/{ruid[8:].upper()}.json"] = json.dumps(fields).encode()

    def content_entry(self, ruid: str) -> dict:
        folder = f"/{ruid[:8].upper()}"
        if folder not in self.dirs:
            self.content_misses += 1
            return {}
        stored = self.files.get(f"{folder}/{ruid[8:].upper()}.json")
        # teddyCloud answers an entry with empty fields when the JSON is missing.
        return json.loads(stored) if stored else {"source": "", "cloud_ruid": "", "cloud_auth": ""}

    def cache_content(self, folder: str, file: str) -> int:
        ruid = f"{folder}{file}".lower()
        self.cloud_requests.append(ruid)
        if self.settings["cloud.enabled"] != "true":
            raise TeddyCloudError("no local content available and cloud access disabled", status=503)
        json_path = f"/{folder}/{file}.json"
        entry = json.loads(self.files.get(json_path, b"{}"))
        if not entry.get("cloud_auth") or ruid not in self.cloud:
            raise TeddyCloudError(f"teddyCloud answered 410 for the download of {folder}/{file}",
                                  status=410)
        taf = self.cloud[ruid]
        if self.settings["cloud.cacheContent"] == "true":
            if self.settings["cloud.cacheToLibrary"] == "true":
                self.dirs.update({"/by", "/by/audioID"})
                name = f"{len(self.cloud_requests)}.taf"
                self.files[f"/by/audioID/{name}"] = taf
                entry["source"] = f"lib://by/audioID/{name}"
                self.files[json_path] = json.dumps(entry).encode()
            else:
                self.files[f"/{folder}/{file}"] = taf
        return len(taf)

    def setting(self, name: str) -> str:
        return self.settings[name]

    def tonies_json(self) -> List[dict]:
        return [dict(entry) for entry in self.catalogue]
