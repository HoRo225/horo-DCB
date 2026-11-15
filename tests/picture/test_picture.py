import io
import zlib

import pytest
from PIL import Image

from horo_dcb import picture
from horo_dcb.blackjack import Card
from horo_dcb.picture import ASSETS, CARD_CORNER, CARD_GAP, CARD_H, CARD_W, CELL, GREEN, HAND_PAD, PAD, RADIUS, Deck, Reel
from horo_dcb.slots import STRIP

HAND_W = 5 * CARD_W + 4 * CARD_GAP


@pytest.fixture(scope="module")
def deck() -> Deck:
    return Deck()


@pytest.fixture(scope="module")
def reel() -> Reel:
    return Reel()


def face(index: int) -> Image.Image:
    raw = zlib.decompress((ASSETS / "cards.bin").read_bytes())
    size = CARD_W * CARD_H * 4
    return Image.frombytes("RGBA", (CARD_W, CARD_H), raw[index * size : (index + 1) * size])


def opened(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def test_card_asset_holds_fifty_two_faces_and_a_back():
    raw = zlib.decompress((ASSETS / "cards.bin").read_bytes())
    assert len(raw) == 53 * CARD_W * CARD_H * 4
    assert len({face(index).tobytes() for index in range(53)}) == 53


def test_hand_lays_cards_left_to_right(deck):
    image = opened(deck.hand([Card("10", "S"), None])).convert("RGBA")
    assert image.size == (HAND_W, CARD_H + 2 * HAND_PAD)
    assert image.crop((0, HAND_PAD, CARD_W, HAND_PAD + CARD_H)).tobytes() == face(9).tobytes()
    left = CARD_W + CARD_GAP
    assert image.crop((left, HAND_PAD, left + CARD_W, HAND_PAD + CARD_H)).tobytes() == face(52).tobytes()
    assert image.crop((2 * left, 0, HAND_W, image.height)).getextrema()[3] == (0, 0)


def test_suits_follow_spades_hearts_diamonds_clubs(deck):
    image = opened(deck.hand([Card("K", "C")])).convert("RGBA")
    assert image.crop((0, HAND_PAD, CARD_W, HAND_PAD + CARD_H)).tobytes() == face(51).tobytes()


def test_more_than_five_cards_overlap_within_the_same_width(deck):
    cards = [Card(rank, "H") for rank in ("2", "3", "4", "5", "6", "7", "8")]
    image = opened(deck.hand(cards)).convert("RGBA")
    assert image.size == (HAND_W, CARD_H + 2 * HAND_PAD)
    step = (HAND_W - CARD_W) // 6
    last = 6 * step
    assert image.crop((last + CARD_CORNER, HAND_PAD, last + CARD_W, HAND_PAD + CARD_H)).tobytes() == face(13 + 7).crop((CARD_CORNER, 0, CARD_W, CARD_H)).tobytes()
    assert image.getpixel((step, HAND_PAD + 3))[3] == 255


def test_still_board_shows_the_stopped_reels(reel):
    stops = (7, 22, 30)
    image = opened(reel.still(stops))
    assert image.mode == "P"
    assert image.size == reel.size
    middle = image.crop((PAD, PAD + CELL + RADIUS, PAD + CELL, PAD + 2 * CELL))
    expected = opened(reel.still((8, 22, 30))).crop((PAD, PAD + RADIUS, PAD + CELL, PAD + CELL))
    assert middle.tobytes() == expected.tobytes()
    assert GREEN not in image.tobytes()


def test_winning_rows_get_a_green_frame(reel):
    image = opened(reel.still((0, 0, 0), (1,)))
    y = PAD + CELL + 2
    assert image.getpixel((reel.size[0] // 2, y)) == GREEN
    assert image.getpixel((reel.size[0] // 2, PAD + 2)) != GREEN


def test_spin_plays_once_and_ends_on_the_still(reel):
    stops, rows = (0, 0, 0), (0, 1, 2)
    image = opened(reel.spin(stops, rows))
    assert "loop" not in image.info
    frames = image.n_frames
    assert frames == int((picture.STOPS_AT[-1] + picture.BOUNCE_TIME) * picture.FPS) + 2
    image.seek(frames - 1)
    assert image.info["duration"] == 60000
    final = image.convert("RGB")
    assert final.tobytes() == opened(reel.still(stops, rows)).convert("RGB").tobytes()


def test_spin_frames_move_before_settling(reel):
    image = opened(reel.spin((5, 10, 15)))
    image.seek(1)
    early = image.convert("RGB").tobytes()
    image.seek(10)
    later = image.convert("RGB").tobytes()
    assert early != later


def test_reel_strip_wraps_around_the_end(reel):
    wrapped = opened(reel.still((len(STRIP) - 1, 0, 0))).crop((PAD, PAD + CELL + RADIUS, PAD + CELL, PAD + 2 * CELL))
    first = opened(reel.still((0, 0, 0))).crop((PAD, PAD + RADIUS, PAD + CELL, PAD + CELL))
    assert wrapped.tobytes() == first.tobytes()
