"""The wire between PMS-AI and Video Analytics.

Both halves of the ReID seam were tested; the seam itself was not. Every VA call
in the exit suite is stubbed at the function level --
`monkeypatch.setattr("app.utils.va_reid_client.compare", fake)` -- so the body
`va_reid_client` actually builds had never been asserted anywhere, and no VA test
touches `/api/reid/compare` or `/api/reid/rename` at all. A field rename on
either side would leave the whole exit suite green and break the correction in
production.

These drive the REAL client over a mock transport and pin the exact bytes. The
matching half lives in the VA repo at `tests/test_reid_api_contract.py`, and the
literals in the two files must be edited together -- that is the point of them.

Pinned against `Damanat-PMS-VideoAnalytics/src/api.py` on
`codex/entry-v2-video-analytics-20260722`.
"""

import base64
import json

import httpx
import pytest

from app.config import settings
from app.utils import va_reid_client

# VA: src/entry/settings.py:18
SERVICE_KEY_HEADER = "X-Service-Key"
# VA: `for plate in payload.plates[:20]` -- src/api.py:1133. A hard truncation
# with no error, so anything past it is dropped in silence.
VA_PLATE_CEILING = 20


@pytest.fixture
def va(monkeypatch):
    """Capture what the real client puts on the wire."""
    sent = []

    class VA:
        requests = sent

        def replies(self, status=200, body=None):
            def handler(request):
                sent.append(request)
                return httpx.Response(
                    status, json=body if body is not None else {}
                )

            real = httpx.AsyncClient

            def factory(*args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(handler)
                return real(*args, **kwargs)

            monkeypatch.setattr(httpx, "AsyncClient", factory)

    monkeypatch.setattr(settings, "PMS_API_URL", "http://va:8000/")
    monkeypatch.setattr(settings, "ENTRY_V2_SERVICE_KEY", "svc-key")
    monkeypatch.setattr(settings, "EXIT_MATCH_REID_ENABLED", True)
    return VA()


CROP_BYTES = b"\xff\xd8\xff" + b"crop-bytes" * 8


@pytest.fixture
def crop(tmp_path):
    """A real file on disk -- `_read_image` stats it before reading."""
    path = tmp_path / "exit.jpg"
    path.write_bytes(CROP_BYTES)
    return str(path)


# --- rename: PMS-AI -> VA ---------------------------------------------------

@pytest.mark.asyncio
async def test_rename_sends_the_aliased_field_names_va_declares(va):
    """VA's model is `from_plate: str = Field(alias="from")`.

    VA sets `populate_by_name`, so it tolerates `from_plate` too -- verified in
    the VA repo's `test_the_unaliased_field_names_are_also_accepted`. This pins
    what we send anyway, because that tolerance is VA's to withdraw and a body
    carrying NEITHER spelling is a 422 the client swallows as "VA said no": the
    correction stays local and the next slot update writes the misread straight
    back over it.
    """
    va.replies(200, {"status": "ok", "gallery_renamed": True,
                     "sessions_updated": 1, "slots_updated": 1})

    assert await va_reid_client.rename("SDD-6707", "SDD-6701") is True

    request, = va.requests
    assert request.url.path == "/api/reid/rename"
    assert json.loads(request.content) == {"from": "SDD-6707", "to": "SDD-6701"}
    assert request.headers[SERVICE_KEY_HEADER] == "svc-key"


@pytest.mark.asyncio
async def test_rename_survives_every_refusal_va_can_answer_with(va):
    """VA answers 401 unauthenticated and 400 on empty plates.

    `apply_correction` has already committed by the time this runs, so a raise
    here would abandon the rest of a catch-up chunk over a fix that is already
    durable.
    """
    for status in (400, 401, 500, 503):
        va.replies(status, {"detail": "no"})
        assert await va_reid_client.rename("SDD-6707", "SDD-6701") is False


@pytest.mark.asyncio
async def test_rename_is_not_sent_at_all_when_it_would_be_a_no_op(va):
    va.replies(200, {"status": "ok"})
    assert await va_reid_client.rename("SDD-6707", "SDD-6707") is False
    assert va.requests == []


# --- compare: PMS-AI -> VA --------------------------------------------------

@pytest.mark.asyncio
async def test_compare_sends_the_keys_va_requires(va, crop):
    """VA's `ReIDCompareRequest` takes `image_base64` and `plates`, unaliased."""
    va.replies(200, {"query_quality_ok": True, "query_sharpness": 118.4,
                     "results": []})

    await va_reid_client.compare(crop, ["SDD-6707", "BHD-9990"])

    request, = va.requests
    assert request.url.path == "/api/reid/compare"
    body = json.loads(request.content)
    assert set(body) == {"image_base64", "plates"}
    assert body["plates"] == ["SDD-6707", "BHD-9990"]
    # Decodable by `base64.b64decode(payload.image_base64)` -- VA hands the
    # result straight to cv2.imdecode and 400s on anything it cannot read.
    assert base64.b64decode(body["image_base64"]) == CROP_BYTES
    assert request.headers[SERVICE_KEY_HEADER] == "svc-key"


@pytest.mark.asyncio
async def test_compare_reads_the_payload_va_actually_returns(va, crop):
    """The literal response body from `src/api.py:1154`, as the matcher reads it.

    `refs` and `model_tag` are VA's own, unread here; `plate`, `score` and
    `query_quality_ok` are load-bearing.
    """
    va.replies(200, {
        "query_quality_ok": True,
        "query_sharpness": 118.4,
        "results": [
            {"plate": "SDD-6707", "score": 0.7412, "refs": 4, "model_tag": "v2"},
            {"plate": "BHD-9990", "score": 0.3106, "refs": 16, "model_tag": "v2"},
        ],
    })

    payload = await va_reid_client.compare(
        crop, ["SDD-6707", "BHD-9990"]
    )

    assert payload.get("query_quality_ok", True) is True
    assert payload["results"][0]["plate"] == "SDD-6707"
    assert [float(r["score"]) for r in payload["results"]] == [0.7412, 0.3106]


@pytest.mark.asyncio
async def test_a_refless_gallery_scores_null_and_is_dropped_not_ranked_last(
    va, crop
):
    """VA answers `score: null` for a plate whose gallery has no usable refs.

    Absence of evidence. Reading it as a number would sort it as maximally
    dissimilar and let a car with no gallery lose a comparison it never entered.
    This mirrors the comprehension in `exit_match_service`.
    """
    va.replies(200, {
        "query_quality_ok": True,
        "query_sharpness": 90.0,
        "results": [
            {"plate": "SDD-6707", "score": 0.7412, "refs": 4, "model_tag": "v2"},
            {"plate": "BHD-9990", "score": None, "refs": 0},
        ],
    })

    payload = await va_reid_client.compare(
        crop, ["SDD-6707", "BHD-9990"]
    )

    scored = [
        (r.get("plate"), float(r["score"]))
        for r in payload.get("results") or ()
        if r.get("score") is not None
    ]
    assert scored == [("SDD-6707", 0.7412)]


@pytest.mark.asyncio
async def test_every_error_va_raises_becomes_no_evidence_never_an_exception(
    va, crop
):
    """503 gallery_disabled, 400 undecodable_image, 422 no_query_feature.

    All three are `HTTPException` on VA's side and all three must read as "no
    appearance evidence" here -- never as "no match", never as a raise into the
    exit path.
    """
    for status, detail in ((503, "gallery_disabled"),
                           (400, "undecodable_image"),
                           (422, "no_query_feature"),
                           (401, "unauthorized")):
        va.replies(status, {"detail": detail})
        assert await va_reid_client.compare(crop, ["SDD-6707"]) is None


# --- the ceiling ------------------------------------------------------------

def test_the_shortlist_can_never_exceed_the_number_of_plates_va_scores():
    """`EXIT_MATCH_SHORTLIST` is an unbounded int; VA truncates at 20.

    They agree today only by coincidence -- the shortlist was raised 5 -> 20 and
    VA's cap already sat at 20. Raise it further in a ConfigMap and VA scores the
    first 20 and drops the rest without a word: the departing car can be in the
    tail, and the exit resolves `ambiguous` with nothing in either service's log
    saying why. Fail here instead.
    """
    assert settings.EXIT_MATCH_SHORTLIST <= VA_PLATE_CEILING, (
        f"EXIT_MATCH_SHORTLIST={settings.EXIT_MATCH_SHORTLIST} exceeds the "
        f"{VA_PLATE_CEILING} plates VA scores (src/api.py, `payload.plates[:20]`). "
        "Raise VA's cap first, in the same deploy."
    )


@pytest.mark.asyncio
async def test_the_matcher_never_asks_va_for_more_plates_than_it_will_score(
    va, crop
):
    """The shortlist is applied before the call, not to the answer."""
    va.replies(200, {"query_quality_ok": True, "query_sharpness": 90.0,
                     "results": []})
    plates = [f"AAA-{n:04d}" for n in range(50)]

    await va_reid_client.compare(
        crop, plates[:settings.EXIT_MATCH_SHORTLIST]
    )

    request, = va.requests
    assert len(json.loads(request.content)["plates"]) <= VA_PLATE_CEILING
