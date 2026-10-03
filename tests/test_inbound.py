import base64

from a2a.types import DataPart, FilePart, FileWithBytes, Part, TextPart

from app import inbound


def _text(t):
    return Part(root=TextPart(text=t))


def _uploads():
    calls = []

    async def upload(data, mime):
        calls.append((data, mime))
        return {"photo_id": f"ph_{len(calls)}", "uri": f"gs://b/{len(calls)}", "mime_type": mime, "bytes": len(data)}

    return upload, calls


async def test_photo_is_staged_and_sentinels_dropped():
    upload, calls = _uploads()
    parts = [
        _text("screen is cracked"),
        _text("screen is cracked"),  # GE has been seen sending the same text twice
        _text("\n<start_of_user_uploaded_file: IMG_1.jpg>"),
        Part(root=FilePart(file=FileWithBytes(bytes=base64.b64encode(b"jpeg").decode(), mime_type="image/jpeg", name="IMG_1.jpg"))),
        _text("<end_of_user_uploaded_file: IMG_1.jpg>\n"),
    ]
    out, photos = await inbound.rewrite_parts(parts, upload)
    texts = [p.root.text for p in out]
    assert texts == ["screen is cracked", "[Photo attached: ph_1]"]
    assert calls == [(b"jpeg", "image/jpeg")]
    assert photos[0]["uri"] == "gs://b/1"


async def test_v09_click_becomes_text_and_drops_the_echo():
    click = {"action": {"name": "select_device", "context": {"asset_tag": "IT-1"}, "sourceComponentId": "c3",
                        "surfaceId": "hw_1", "timestamp": "2026-09-30T17:11:23.737Z"}}
    parts = [_text(inbound.CLICK_ECHO), Part(root=DataPart(data=click, metadata={"mimeType": "application/json+a2ui"}))]
    out, _ = await inbound.rewrite_parts(parts, _uploads()[0])
    assert [p.root.text for p in out] == ['[UI action] select_device {"asset_tag": "IT-1"}']


async def test_echo_text_alone_is_kept():
    out, _ = await inbound.rewrite_parts([_text(inbound.CLICK_ECHO)], _uploads()[0])
    assert out[0].root.text == inbound.CLICK_ECHO


async def test_user_action_becomes_text_for_both_context_shapes():
    listed = {"userAction": {"name": "select_device",
                             "context": [{"key": "asset_tag", "value": {"literalString": "IT-1"}}]}}
    flat = {"userAction": {"name": "select_device", "context": {"asset_tag": "IT-1"}}}
    for data in (listed, flat):
        out, _ = await inbound.rewrite_parts([Part(root=DataPart(data=data))], _uploads()[0])
        assert out[0].root.text == '[UI action] select_device {"asset_tag": "IT-1"}'


async def test_empty_message_still_has_a_part():
    out, _ = await inbound.rewrite_parts([_text("<start_of_user_uploaded_file: x.pdf>")], _uploads()[0])
    assert len(out) == 1 and out[0].root.text


def test_requested_a2ui_version_picks_the_highest():
    v = inbound.requested_a2ui_version
    assert v(["https://a2ui.org/a2a-extension/a2ui/v0.8"]) == "0.8"
    assert v(["https://a2ui.org/a2a-extension/a2ui/v0.9", "https://google.github.io/adk-docs/a2a/a2a-extension/"]) == "0.9"
    assert v(["https://a2ui.org/a2a-extension/a2ui/v0.8", "https://a2ui.org/a2a-extension/a2ui/v0.9"]) == "0.9"
    assert v([]) == ""


def _photo(data=b"jpeg", mime="image/jpeg", raw=None):
    encoded = raw if raw is not None else base64.b64encode(data).decode()
    return Part(root=FilePart(file=FileWithBytes(bytes=encoded, mime_type=mime, name="IMG.jpg")))


async def test_a_photo_that_cannot_be_saved_keeps_the_text():
    async def broken(data, mime):
        raise RuntimeError("GCS 503")

    out, photos = await inbound.rewrite_parts([_text("screen is cracked"), _photo()], broken)
    texts = [p.root.text for p in out]
    assert texts[0] == "screen is cracked" and "could not be saved" in texts[1] and photos == []


async def test_bad_photos_never_fail_the_turn():
    upload, calls = _uploads()
    parts = [_text("help"), _photo(raw="!!!not base64!!!"), _photo(b""), _photo(mime="image/svg+xml"),
             _photo(b"x" * (inbound.MAX_PHOTO_BYTES + 1))]
    out, photos = await inbound.rewrite_parts(parts, upload)
    texts = " ".join(p.root.text for p in out)
    assert photos == [] and calls == []
    assert "Unsupported attachment" in texts and "too large" in texts and texts.startswith("help")


async def test_a_file_sent_as_a_link_is_explained_not_passed_to_the_model():
    from a2a.types import FileWithUri

    upload, _ = _uploads()
    part = Part(root=FilePart(file=FileWithUri(uri="blobstore://x/y", mime_type="image/jpeg", name="IMG.jpg")))
    out, photos = await inbound.rewrite_parts([part], upload)
    assert isinstance(out[0].root, TextPart) and "link" in out[0].root.text and photos == []
