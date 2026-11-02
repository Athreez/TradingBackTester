from core.registry import available_strategies, load_strategy


def test_triple_iron_fly_is_discoverable():
    assert "triple_iron_fly" in available_strategies()
    strategy, config = load_strategy("triple_iron_fly")
    assert set(config["underlyings"]) <= {"NIFTY", "SENSEX"}
    assert isinstance(strategy.params, dict)
