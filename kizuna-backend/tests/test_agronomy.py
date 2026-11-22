"""Agronomy core checked against FAO-56 worked examples and equations."""
import pytest

from app.agronomy import et0, kc, water_balance as wb
from app.agronomy.reference_data import CROPS, SOILS


def test_extraterrestrial_radiation_fao_example_8():
    # 3 September (J=246), 20 deg S -> Ra = 32.2 MJ m-2 day-1
    assert et0.extraterrestrial_radiation(-20.0, 246) == pytest.approx(32.2, abs=0.1)


def test_penman_monteith_fao_example_18_brussels():
    u2 = et0.wind_speed_2m(2.78, 10.0)
    assert u2 == pytest.approx(2.08, abs=0.01)
    assert et0.extraterrestrial_radiation(50.8, 187) == pytest.approx(41.09, abs=0.05)
    value = et0.et0_penman_monteith(tmax=21.5, tmin=12.3, u2=u2, latitude_deg=50.8, altitude_m=100,
                                    day_of_year=187, rh_max=84, rh_min=63, sunshine_hours=9.25)
    assert value == pytest.approx(3.9, abs=0.05)


def test_vapour_pressure_example_18():
    assert et0.mean_saturation_vapour_pressure(21.5, 12.3) == pytest.approx(1.997, abs=0.005)
    assert et0.actual_vapour_pressure_from_rh(21.5, 12.3, 84, 63) == pytest.approx(1.409, abs=0.005)


def test_hargreaves_is_positive_and_reasonable():
    v = et0.et0_hargreaves(tmax=34, tmin=20, latitude_deg=36.8, day_of_year=196)  # Tunis, mid-July
    assert 5.0 < v < 9.0


def curve(key="tomato"):
    c = CROPS[key]
    return kc.KcCurve(c.kc_ini, c.kc_mid, c.kc_end, c.l_ini, c.l_dev, c.l_mid, c.l_late)


def test_kc_curve_shape_tomato():
    c = curve()
    assert kc.kc_for_day(c, 0) == 0.60
    assert kc.kc_for_day(c, 29) == 0.60
    assert kc.kc_for_day(c, 50) == pytest.approx(0.60 + (20 / 40) * (1.15 - 0.60))  # Eq. 66
    assert kc.kc_for_day(c, 80) == 1.15
    assert kc.kc_for_day(c, 200) == 0.80
    assert kc.stage_for_day(c, 75).stage == "mid_season"
    assert kc.stage_for_day(c, 75).days_to_next_stage == 40


def test_kc_mid_climate_adjustment_neutral_and_dry():
    assert kc.adjust_kc_mid(1.15, 2.0, 45.0, 0.6) == pytest.approx(1.15)
    dry = kc.adjust_kc_mid(1.15, 3.0, 25.0, 0.6)
    expected = 1.15 + (0.04 * 1 - 0.004 * (-20)) * (0.6 / 3) ** 0.3
    assert dry == pytest.approx(expected)
    assert dry > 1.15


def test_water_balance_equations():
    loam = SOILS["loam"]
    assert loam.theta_fc == 0.25 and loam.theta_wp == 0.12
    taw = wb.total_available_water(0.25, 0.12, 0.7)
    assert taw == pytest.approx(91.0)
    assert wb.adjusted_depletion_fraction(0.40, 5.0) == pytest.approx(0.40)
    assert wb.adjusted_depletion_fraction(0.40, 7.0) == pytest.approx(0.32)
    assert wb.adjusted_depletion_fraction(0.40, 30.0) == pytest.approx(0.1)  # lower limit
    assert wb.depletion_from_soil_moisture(0.25, 0.25, 0.7, taw) == 0
    assert wb.depletion_from_soil_moisture(0.18, 0.25, 0.7, taw) == pytest.approx(49.0)
    assert wb.depletion_from_soil_moisture(0.01, 0.25, 0.7, taw) == pytest.approx(taw)  # capped
    assert wb.water_stress_coefficient(20, 91, 36.4, 0.4) == 1.0
    assert wb.water_stress_coefficient(91, 91, 36.4, 0.4) == 0.0


def test_effective_rain_threshold():
    assert wb.effective_rainfall(0.8, 6.0) == 0.0     # < 0.2 ET0 -> evaporated
    assert wb.effective_rainfall(5.0, 6.0) == 5.0


def test_daily_step_deep_percolation():
    step = wb.daily_step(10.0, etc_mm=5.0, rain_eff_mm=20.0, irrigation_net_mm=0.0, taw=90.0)
    assert step.dr_end == 0.0 and step.deep_percolation == pytest.approx(5.0)


def test_volume_conversion():
    gross = wb.gross_irrigation_mm(18.0, 0.9)
    assert gross == pytest.approx(20.0)
    assert wb.mm_to_litres(gross, 10.0) == pytest.approx(200.0)
