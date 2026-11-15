import pytest

from horo_dcb.blackjack import Card, Round, payout, shoe, total


def dealt(*draws: str) -> Round:
    return Round([Card(draw[:-1], draw[-1]) for draw in reversed(draws)])


@pytest.mark.parametrize(
    ("result", "stake", "returned"),
    [("win", 100, 200), ("blackjack", 100, 250), ("blackjack", 25, 62), ("blackjack", 11, 27), ("push", 100, 100), ("lose", 100, 0), ("bust", 100, 0)],
)
def test_payout_returns_stake_plus_winnings(result, stake, returned):
    assert payout(result, stake) == returned


def test_shoe_holds_six_decks():
    cards = shoe()
    assert len(cards) == 312
    assert cards.count(Card("A", "S")) == 6


@pytest.mark.parametrize(("cards", "expected"), [(["AS", "6H"], (17, True)), (["AS", "6H", "KD"], (17, False)), (["AS", "AH", "9D"], (21, True)), (["KS", "QH", "5D"], (25, False))])
def test_aces_count_as_one_only_when_needed(cards, expected):
    assert total([Card(card[:-1], card[-1]) for card in cards]) == expected


def test_player_blackjack_ends_the_round_at_once():
    hand = dealt("AS", "9H", "KD", "7C")
    assert hand.done and hand.result() == "blackjack"


def test_both_blackjacks_push():
    hand = dealt("AS", "AH", "KD", "QC")
    assert hand.done and hand.result() == "push"


def test_dealer_blackjack_beats_a_player_twenty():
    hand = dealt("KS", "AH", "QD", "JC")
    assert hand.done and hand.result() == "lose"


def test_bust_ends_without_dealer_drawing():
    hand = dealt("KS", "6H", "6D", "JC", "QS", "5H")
    hand.apply("hit")
    assert hand.done and hand.result() == "bust"
    assert len(hand.dealer) == 2


def test_reaching_twenty_one_stands_automatically():
    hand = dealt("5S", "KH", "6D", "7C", "QS", "2H")
    hand.apply("hit")
    assert hand.done
    assert [card.rank for card in hand.dealer] == ["K", "7"]
    assert hand.result() == "win"


def test_dealer_draws_to_seventeen_and_stands_on_soft_seventeen():
    hand = dealt("10S", "6H", "9D", "QC", "AS", "5H")
    hand.apply("stand")
    assert [card.rank for card in hand.dealer] == ["6", "Q", "A"]
    assert hand.result() == "win"
    soft = dealt("10S", "AH", "8D", "6C", "KS")
    soft.apply("stand")
    assert [card.rank for card in soft.dealer] == ["A", "6"]
    assert soft.result() == "win"


def test_equal_points_push_and_dealer_bust_wins():
    tied = dealt("10S", "10H", "8D", "8C")
    tied.apply("stand")
    assert tied.result() == "push"
    busted = dealt("10S", "10H", "7D", "6C", "KS")
    busted.apply("stand")
    assert busted.result() == "win"


def test_finished_round_refuses_more_moves():
    hand = dealt("AS", "9H", "KD", "7C")
    with pytest.raises(ValueError):
        hand.apply("hit")
