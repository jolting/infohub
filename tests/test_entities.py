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


from infohub.entities import level_control


def state(entity_id, on=True, **attributes):
    return SimpleNamespace(entity_id=entity_id, state="on" if on else "off", attributes=attributes)


def test_dimmable_light():
    assert level_control(state("light.desk", brightness=128, supported_color_modes=["brightness"])) == (50, 1, 100)
    assert level_control(state("light.desk", on=False, supported_color_modes=["color_temp"])) == (0, 1, 100)


def test_on_off_light_is_a_toggle():
    assert level_control(state("light.plug", supported_color_modes=["onoff"])) is None
    assert level_control(state("light.old")) is None


def test_fan_speed():
    fan_state = state("fan.office", percentage=33, percentage_step=16.666666666666668, supported_features=61)
    assert level_control(fan_state) == (2, 1, 6)


def test_humidifier_target():
    h = state("humidifier.big", humidity=55, min_humidity=40, max_humidity=80)
    assert level_control(h) == (55, 40, 80)


def test_switch_has_no_level():
    assert level_control(state("switch.printer")) is None


from infohub.entities import daily_from_twice_daily


def test_twice_daily_becomes_daily():
    periods = [
        {"datetime": "2026-11-25T18:00", "is_daytime": False, "temperature": 45, "condition": "clear-night"},
        {"datetime": "2026-11-26T06:00", "is_daytime": True, "temperature": 72, "condition": "sunny"},
        {"datetime": "2026-11-26T18:00", "is_daytime": False, "temperature": 47, "condition": "clear-night"},
        {"datetime": "2026-11-27T06:00", "is_daytime": True, "temperature": 70, "condition": "cloudy"},
    ]
    days = daily_from_twice_daily(periods)
    assert [(d["temperature"], d["templow"]) for d in days] == [(None, 45), (72, 47), (70, None)]
    assert days[1]["condition"] == "sunny" and "is_daytime" not in days[1]
