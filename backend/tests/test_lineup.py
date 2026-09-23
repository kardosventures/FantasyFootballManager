from app.lineup import PlayerOption, optimize_lineup


def test_two_flex_optimizer_avoids_inactive_and_preserves_late_player_in_flex():
    players = [
        PlayerOption("qb", "QB", 20),
        PlayerOption("rb1", "RB", 15, kickoff_epoch=100),
        PlayerOption("rb2", "RB", 14, kickoff_epoch=200),
        PlayerOption("wr1", "WR", 13, kickoff_epoch=300),
        PlayerOption("wr2", "WR", 12, kickoff_epoch=400),
        PlayerOption("wr3", "WR", 11, kickoff_epoch=500),
        PlayerOption("te", "TE", 10, kickoff_epoch=600),
        PlayerOption("flex-late", "WR", 9, kickoff_epoch=900),
        PlayerOption("flex-early", "RB", 8, kickoff_epoch=50),
        PlayerOption("inactive-star", "WR", 50, inactive=True),
    ]
    lineup = optimize_lineup(players, ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "FLEX"])
    assert "inactive-star" not in lineup
    assert lineup[-1] == "flex-late" or lineup[-2] == "flex-late"


def test_locked_player_stays_in_exact_slot():
    players = [
        PlayerOption("locked", "WR", 1, locked=True, locked_slot=0),
        PlayerOption("better", "WR", 20),
    ]
    assert optimize_lineup(players, ["WR"])[0] == "locked"


def test_optimizer_honors_multi_position_eligibility():
    players = [
        PlayerOption("hybrid", "DB", 12, eligible_positions=("WR",)),
        PlayerOption("receiver", "WR", 10),
    ]
    assert optimize_lineup(players, ["WR"])[0] == "hybrid"
