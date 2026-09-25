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
