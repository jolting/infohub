from types import SimpleNamespace

from infohub.entities import fan_speeds


def fan(entity_id="fan.office", step=16.666666666666668, features=61):
    return SimpleNamespace(
        entity_id=entity_id,
        attributes={"percentage_step": step, "supported_features": features},
    )


def test_six_speed_fan():
    assert fan_speeds(fan()) == 6


def test_fan_without_set_speed_is_a_toggle():
    assert fan_speeds(fan(features=48)) is None  # turn on/off only


def test_not_a_fan():
    assert fan_speeds(fan(entity_id="light.office")) is None


def test_no_step_means_whole_percents():
    assert fan_speeds(fan(step=None)) == 100


def test_speed_and_percentage_round_trip():
    """The slider's position (coordinator) and the percentage it sets
    (__init__.py) agree for every speed of the R1's 6-speed fans, with
    the percentages Home Assistant reports for them."""
    speeds = 6
    for speed, reported in enumerate([0, 16, 33, 50, 66, 83, 100]):
        assert round(reported * speeds / 100) == speed
        if speed:
            assert min(100, round(speed * 100 / speeds)) in (17, 33, 50, 67, 83, 100)
