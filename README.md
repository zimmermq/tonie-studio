# tonie-studio

Turns an album folder into a playable Traveltonie: pick the tracks, encode them into a taf, put
them on an NFC tag, give the tag a cover, and print the round stickers that go on the tags.

It is a companion to [teddyCloud](https://github.com/toniebox-reverse-engineering/teddycloud),
not a replacement. teddyCloud can do every one of those steps, but each in a different screen
with its own vocabulary, and the last one not at all. This walks the whole way once, in the order
a person actually works.

## The pieces it puts together

A Traveltonie is three things that teddyCloud keeps apart:

- a **taf** in the audio library, the encoded audio, one chapter per track
- a **tag entry** under `content/<CN>/<UID>.json`, which points a physical NFC tag at that taf
- a **custom model** in `tonies.custom.json`, which supplies the cover and the track names
  teddyCloud shows, and is attached to the tag through its `tonie_model` field

The physical half is a 46 mm round sticker with the album cover, glued onto the tag. That sheet
is the part no tool produces, and the reason this one exists.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
```

`TS_TEDDYCLOUD_URL` points at the teddyCloud instance, for example
`http://teddycloud-service.argocd.svc.cluster.local` in the cluster or a `kubectl port-forward`
locally. `TS_TEDDYCLOUD_OVERLAY` selects a box configuration and is only needed with more than
one box.

## A tag has to be known before it can be filled

teddyCloud learns an NFC tag the first time the Toniebox reads it. A fresh sticker out of the bag
is therefore invisible until it has been held against the box once, and no amount of API calls
brings it forward. The workflow for a new Traveltonie starts at the box, not here.

## Tests

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/ -v
.venv/bin/python scripts/check_chars.py src tests scripts
```
