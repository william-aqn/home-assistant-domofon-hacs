"""Tests for device parsing and stream URL normalisation.

The URL cases below reproduce the *shapes* the backend really returns, including its
self-contradictory ones. Hosts and channel ids are placeholders on purpose.
"""

from __future__ import annotations

import pytest

from custom_components.loki.models import (
    LokiDevice,
    normalize_stream,
    parse_device_list,
)

HOST = "203.0.113.10"
CHANNEL = "ch316m39uzftqhnxceqeyzo"


@pytest.mark.parametrize(
    "raw",
    [
        # Plain HTTP form, the most common one.
        f"http://{HOST}:8888/{CHANNEL}",
        # With the inline credentials the backend sometimes embeds.
        f"http://1:1@{HOST}:8888/{CHANNEL}",
        f"http://1:2@{HOST}:8888/{CHANNEL}",
        # Genuinely inconsistent backend data: an rtsp:// scheme on the HTTP port.
        f"rtsp://1:2@{HOST}:8888/{CHANNEL}",
        # The honest RTSP form.
        f"rtsp://{HOST}:8554/{CHANNEL}",
        # HLS variant, as the official app builds it.
        f"http://{HOST}:8888/{CHANNEL}/index.m3u8",
        # No scheme at all — exercises the fallback parser.
        f"{HOST}:8888/{CHANNEL}",
        # Stray whitespace.
        f"  http://{HOST}:8888/{CHANNEL}  ",
    ],
)
def test_normalize_stream_ignores_untrustworthy_scheme_port_and_credentials(
    raw: str,
) -> None:
    """Only the hostname and channel are trusted; everything else is rebuilt."""
    stream = normalize_stream(raw)

    assert stream is not None
    assert stream.host == HOST
    assert stream.channel == CHANNEL
    assert stream.rtsp == f"rtsp://{HOST}:8554/{CHANNEL}"
    assert stream.hls == f"http://{HOST}:8888/{CHANNEL}/index.m3u8"


@pytest.mark.parametrize("raw", ["", "   ", None, "http://", "not a url", "/only/path"])
def test_normalize_stream_returns_none_when_unusable(raw: str | None) -> None:
    """A device with no usable stream must not get a camera entity."""
    assert normalize_stream(raw) is None


def test_normalize_stream_takes_first_path_segment() -> None:
    """Matches the reference client, which splits on '/' and takes the first segment.

    Channels are single-segment today, so this only matters if that ever changes --
    at which point behaving like the client known to work beats diverging silently.
    """
    stream = normalize_stream(f"http://{HOST}:8888/{CHANNEL}/index.m3u8")

    assert stream is not None
    assert stream.channel == CHANNEL


def test_device_from_api_treats_accumulated_img_suffixes_as_a_flag_only() -> None:
    """The img field degrades into an invalid URL, so it is only a boolean signal."""
    device = LokiDevice.from_api(
        {
            "id": 77,
            "name": " Калитка выход",
            "rname": "Комплекс->Строение 2",
            # Real backend data looks like this: the cache-busting suffix is appended
            # over and over until the value stops being a valid URL.
            "img": "/device/77.jpg?a=d1d06bac?a=77be9718?a=648386d6?a=1d280da7",
            "url": f"http://1:1@{HOST}:8888/{CHANNEL}",
        },
        is_door=True,
    )

    assert device is not None
    assert device.has_thumbnail is True
    assert device.name == "Калитка выход"
    assert device.area == "Строение 2"
    assert device.stream is not None


def test_device_without_stream_or_thumbnail() -> None:
    """Doors with no camera are common and must parse cleanly."""
    device = LokiDevice.from_api(
        {"id": 81, "name": "Калитка 3", "rname": "Комплекс", "img": "", "url": ""},
        is_door=True,
    )

    assert device is not None
    assert device.stream is None
    assert device.has_thumbnail is False
    assert device.area == "Комплекс"


def test_device_from_api_rejects_entries_without_a_numeric_id() -> None:
    """An id is the only field we cannot work around."""
    assert LokiDevice.from_api({"name": "x"}, is_door=True) is None
    assert LokiDevice.from_api({"id": "695", "name": "x"}, is_door=True) is None


def test_device_from_api_rejects_boolean_ids() -> None:
    """bool subclasses int, and True would collide with device 1."""
    assert LokiDevice.from_api({"id": True, "name": "x"}, is_door=True) is None
    assert LokiDevice.from_api({"id": False, "name": "x"}, is_door=True) is None


@pytest.mark.parametrize(
    "bad_url",
    [
        # urlsplit raises ValueError on a bracketed host that is not valid IPv6.
        "http://[not-ipv6]/ch123",
        # The url field arrives off the wire; it is not guaranteed to be a string.
        12345,
        {"nested": "object"},
        ["list"],
    ],
)
def test_device_survives_an_unusable_url(bad_url: object) -> None:
    """One malformed url must not cost us the device, let alone the whole list.

    Before this guard a single bad entry propagated out of the coordinator and made
    every door and camera unavailable.
    """
    device = LokiDevice.from_api(
        {"id": 695, "name": "Центральный вход", "img": "", "url": bad_url},
        is_door=True,
    )

    assert device is not None
    assert device.id == 695
    assert device.stream is None


def test_parse_device_list_survives_a_poisoned_entry() -> None:
    """A bad url on one device must not remove the others."""
    devices = parse_device_list(
        {
            "dom": [
                {"id": 1, "name": "Плохая", "img": "", "url": "http://[not-ipv6]/ch"},
                {
                    "id": 2,
                    "name": "Хорошая",
                    "img": "",
                    "url": f"http://{HOST}:8888/{CHANNEL}",
                },
            ]
        }
    )

    by_id = {device.id: device for device in devices}
    assert set(by_id) == {1, 2}
    assert by_id[1].stream is None
    assert by_id[2].stream is not None


def test_parse_device_list_duplicate_resolution_is_order_independent() -> None:
    """Among same-role duplicates the first entry wins, whichever array it is in."""
    first = {"id": 5, "name": "A", "img": "", "url": ""}
    second = {"id": 5, "name": "B", "img": "", "url": f"http://{HOST}:8888/{CHANNEL}"}

    doors = parse_device_list({"dom": [first, second]})
    cameras = parse_device_list({"video": [first, second]})

    assert [d.name for d in doors] == ["A"]
    assert [d.name for d in cameras] == ["A"]


def test_stream_urls_bracket_ipv6_hosts() -> None:
    """A bare IPv6 literal is not a valid URL authority without brackets."""
    stream = normalize_stream("http://[2001:db8::1]:8888/ch123")

    assert stream is not None
    assert stream.host == "2001:db8::1"
    assert stream.rtsp == "rtsp://[2001:db8::1]:8554/ch123"
    assert stream.hls == "http://[2001:db8::1]:8888/ch123/index.m3u8"


def test_parse_device_list_splits_doors_from_cameras() -> None:
    """Door vs camera is decided purely by which array the entry arrived in."""
    devices = parse_device_list(
        {
            "dom": [{"id": 695, "name": "Центральный вход", "img": "", "url": ""}],
            "video": [
                {
                    "id": 213,
                    "name": "Камера 01",
                    "img": "",
                    "url": f"http://1:1@{HOST}:8888/{CHANNEL}",
                }
            ],
        }
    )

    by_id = {device.id: device for device in devices}
    assert by_id[695].is_door is True
    assert by_id[213].is_door is False


def test_parse_device_list_prefers_the_door_role_on_conflict() -> None:
    """Doors are the only devices lockOpen accepts, so that role must win."""
    devices = parse_device_list(
        {
            "dom": [{"id": 695, "name": "Центральный вход", "img": "", "url": ""}],
            "video": [{"id": 695, "name": "Центральный вход", "img": "", "url": ""}],
        }
    )

    assert len(devices) == 1
    assert devices[0].is_door is True


def test_parse_device_list_tolerates_missing_and_malformed_arrays() -> None:
    """A partial response should degrade, not explode."""
    assert parse_device_list({}) == []
    assert parse_device_list({"dom": None, "video": "nope"}) == []
    assert parse_device_list({"dom": [None, 42, {"id": 1, "name": "ok"}]}) != []
