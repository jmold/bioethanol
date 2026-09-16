from __future__ import annotations

import math
from dataclasses import dataclass, asdict

MW_ETHANOL = 46.06844
MW_WATER = 18.01528

# NIST Antoine coefficients, log10(P_bar)=A-B/(T_K+C).
# Ethanol: Ambrose & Sprake (1970), 292.77-366.63 K.
# Water: Stull-style range used only for shortcut saturation-temperature iteration.
ETHANOL_ANTOINE_RANGES = [
    (273.0, 351.70, (5.36658, 1670.409, -40.191)),
    (292.77, 366.63, (5.24106, 1598.673, -46.424)),
    (364.8, 513.91, (4.91960, 1432.526, -61.819)),
]
WATER_ANTOINE_RANGES = [
    (255.9, 373.0, (4.6486, 1435.264, -64.848)),
    (344.0, 373.0, (5.07783, 1663.125, -45.622)),
    (379.0, 573.0, (3.55388, 643.748, -198.043)),
]

LATENT_ETHANOL_KJ_KG = 846.0
LATENT_WATER_KJ_KG = 2257.0


@dataclass
class ShortcutResult:
    feed_ethanol_mole_fraction: float
    distillate_ethanol_mole_fraction: float
    bottoms_ethanol_mole_fraction: float
    representative_temperature_C: float
    relative_volatility: float
    minimum_stages_fenske: float
    minimum_reflux_underwood: float
    required_theoretical_stages_gilliland: float
    installed_actual_trays: float
    overall_tray_efficiency_fraction: float
    installed_effective_stages: float
    stage_margin: float
    reflux_ratio: float
    reflux_to_minimum_ratio: float
    stage_feasible: bool
    reflux_feasible: bool
    estimated_reboiler_kW: float
    estimated_condenser_kW: float
    method: str = "Fenske-Underwood-Gilliland binary ethanol/water shortcut"
    status: str = "MECHANISTIC SHORTCUT / NOT RIGOROUS RATE-BASED VLE"

    def to_dict(self):
        result = asdict(self)
        for key, value in result.items():
            if isinstance(value, float) and not math.isfinite(value):
                result[key] = "Infinity" if value > 0 else "-Infinity" if value < 0 else "NaN"
        return result


def wt_to_mole_fraction(w_ethanol: float) -> float:
    w = min(max(float(w_ethanol), 1e-12), 1 - 1e-12)
    n_e = w / MW_ETHANOL
    n_w = (1 - w) / MW_WATER
    return n_e / (n_e + n_w)


def _coeffs_for_temperature(t_k: float, ranges):
    containing=[row for row in ranges if row[0] <= t_k <= row[1]]
    if containing:
        return min(containing,key=lambda row:row[1]-row[0])[2]
    return min(ranges,key=lambda row:min(abs(t_k-row[0]),abs(t_k-row[1])))[2]


def _psat_atm(t_k: float, ranges) -> float:
    a,b,c=_coeffs_for_temperature(t_k,ranges)
    return 10 ** (a - b / (t_k + c))


def saturation_temperature_C(pressure_bar: float, ranges) -> float:
    target_atm=max(float(pressure_bar)/1.01325,0.05)
    lo,hi=273.15,473.15
    for _ in range(120):
        mid=(lo+hi)/2
        if _psat_atm(mid,ranges)<target_atm:
            lo=mid
        else:
            hi=mid
    return (lo+hi)/2-273.15


def representative_relative_volatility(feed_ethanol_mole_fraction: float, pressure_bar: float) -> tuple[float,float]:
    t_e=saturation_temperature_C(pressure_bar,ETHANOL_ANTOINE_RANGES)
    t_w=saturation_temperature_C(pressure_bar,WATER_ANTOINE_RANGES)
    x=min(max(feed_ethanol_mole_fraction,0.0),1.0)
    t_c=x*t_e+(1-x)*t_w
    t_k=t_c+273.15
    alpha=_psat_atm(t_k,ETHANOL_ANTOINE_RANGES)/max(_psat_atm(t_k,WATER_ANTOINE_RANGES),1e-12)
    return max(alpha,1.01),t_c


def fenske_min_stages(xd: float, xb: float, alpha: float) -> float:
    xd=min(max(xd,1e-9),1-1e-9); xb=min(max(xb,1e-9),1-1e-9)
    sep=(xd/(1-xd))*((1-xb)/xb)
    return max(0.0,math.log(max(sep,1.0))/math.log(max(alpha,1.000001)))


def underwood_min_reflux(zf: float, xd: float, alpha: float, q: float=1.0) -> float:
    # Saturated-liquid binary Underwood form:
    # sum(alpha_i*z_i/(alpha_i-theta)) = 1-q, root between heavy and light key alphas.
    lo,hi=1.0+1e-9,alpha-1e-9
    target=1.0-q
    for _ in range(120):
        theta=(lo+hi)/2
        value=alpha*zf/(alpha-theta)+(1-zf)/(1-theta)-target
        if value>0.0:
            hi=theta
        else:
            lo=theta
    theta=(lo+hi)/2
    rmin=xd*alpha/(alpha-theta)+(1-xd)/(1-theta)-1.0
    return max(0.0,rmin)


def gilliland_required_stages(nmin: float, rmin: float, reflux_ratio: float) -> float:
    r=max(float(reflux_ratio),0.0)
    if r <= rmin + 1e-9:
        return float("inf")
    x=(r-rmin)/(r+1.0)
    x=min(max(x,1e-9),0.999999)
    y=1.0-math.exp(((1+54.4*x)/(11+117.2*x))*((x-1)/math.sqrt(x)))
    y=min(max(y,0.0),0.999999)
    return (nmin+y)/(1-y)


def shortcut_column(
    *,
    feed_ethanol_wt_fraction: float,
    distillate_ethanol_wt_fraction: float,
    bottoms_ethanol_wt_fraction: float,
    pressure_bar_abs: float,
    actual_trays: float,
    tray_efficiency_fraction: float,
    reflux_ratio: float,
    distillate_tph: float,
) -> ShortcutResult:
    zf=wt_to_mole_fraction(feed_ethanol_wt_fraction)
    xd=wt_to_mole_fraction(distillate_ethanol_wt_fraction)
    xb=wt_to_mole_fraction(bottoms_ethanol_wt_fraction)
    alpha,t_c=representative_relative_volatility(zf,pressure_bar_abs)
    nmin=fenske_min_stages(xd,xb,alpha)
    rmin=underwood_min_reflux(zf,xd,alpha)
    nreq=gilliland_required_stages(nmin,rmin,reflux_ratio)
    neff=max(0.0,float(actual_trays))*min(max(float(tray_efficiency_fraction),0.0),1.0)
    stage_margin=neff-nreq if math.isfinite(nreq) else float("-inf")

    # Constant-molar-overflow screening duty from overhead vapour traffic V ~= D(R+1).
    xd_mass=min(max(float(distillate_ethanol_wt_fraction),0.0),1.0)
    latent=xd_mass*LATENT_ETHANOL_KJ_KG+(1-xd_mass)*LATENT_WATER_KJ_KG
    vapour_tph=max(0.0,float(distillate_tph))*(max(float(reflux_ratio),0.0)+1.0)
    duty=vapour_tph*1000.0*latent/3600.0

    return ShortcutResult(
        feed_ethanol_mole_fraction=zf,
        distillate_ethanol_mole_fraction=xd,
        bottoms_ethanol_mole_fraction=xb,
        representative_temperature_C=t_c,
        relative_volatility=alpha,
        minimum_stages_fenske=nmin,
        minimum_reflux_underwood=rmin,
        required_theoretical_stages_gilliland=nreq,
        installed_actual_trays=float(actual_trays),
        overall_tray_efficiency_fraction=float(tray_efficiency_fraction),
        installed_effective_stages=neff,
        stage_margin=stage_margin,
        reflux_ratio=float(reflux_ratio),
        reflux_to_minimum_ratio=(float(reflux_ratio)/rmin if rmin>0 else float("inf")),
        stage_feasible=math.isfinite(nreq) and neff+1e-9>=nreq,
        reflux_feasible=float(reflux_ratio)>rmin,
        estimated_reboiler_kW=duty,
        estimated_condenser_kW=duty,
    )
