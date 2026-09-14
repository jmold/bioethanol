from __future__ import annotations

import math
from dataclasses import dataclass, asdict

MW_ETHANOL = 46.06844
MW_WATER = 18.01528

# NIST Antoine coefficients, log10(P_bar)=A-B/(T_K+C).
# Ethanol: Ambrose & Sprake (1970), 292.77-366.63 K.
# Water: Stull-style range used only for shortcut saturation-temperature iteration.
ETHANOL_ANTOINE = (5.24677, 1598.673, -46.424)
WATER_ANTOINE = (5.40221, 1838.675, -31.737)

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
        return asdict(self)


def wt_to_mole_fraction(w_ethanol: float) -> float:
    w = min(max(float(w_ethanol), 1e-12), 1 - 1e-12)
    n_e = w / MW_ETHANOL
    n_w = (1 - w) / MW_WATER
    return n_e / (n_e + n_w)


def _psat_bar(t_k: float, coeffs: tuple[float,float,float]) -> float:
    a,b,c = coeffs
    return 10 ** (a - b / (t_k + c))


def saturation_temperature_C(pressure_bar: float, coeffs: tuple[float,float,float]) -> float:
    target=max(float(pressure_bar),0.05)
    lo,hi=273.15,473.15
    for _ in range(100):
        mid=(lo+hi)/2
        if _psat_bar(mid,coeffs)<target:
            lo=mid
        else:
            hi=mid
    return (lo+hi)/2-273.15


def representative_relative_volatility(feed_ethanol_mole_fraction: float, pressure_bar: float) -> tuple[float,float]:
    t_e=saturation_temperature_C(pressure_bar,ETHANOL_ANTOINE)
    t_w=saturation_temperature_C(pressure_bar,WATER_ANTOINE)
    x=min(max(feed_ethanol_mole_fraction,0.0),1.0)
    t_c=x*t_e+(1-x)*t_w
    t_k=t_c+273.15
    alpha=_psat_bar(t_k,ETHANOL_ANTOINE)/max(_psat_bar(t_k,WATER_ANTOINE),1e-12)
    return max(alpha,1.01),t_c


def fenske_min_stages(xd: float, xb: float, alpha: float) -> float:
    xd=min(max(xd,1e-9),1-1e-9); xb=min(max(xb,1e-9),1-1e-9)
    sep=(xd/(1-xd))*((1-xb)/xb)
    return max(0.0,math.log(max(sep,1.0))/math.log(max(alpha,1.000001)))


def underwood_min_reflux(zf: float, xd: float, alpha: float, q: float=1.0) -> float:
    # Binary light key ethanol (alpha) / heavy key water (1.0).
    lo,hi=1.0+1e-9,alpha-1e-9
    for _ in range(100):
        theta=(lo+hi)/2
        value=q*zf/(alpha-theta)+q*(1-zf)/(1-theta)
        if value>1.0:
            lo=theta
        else:
            hi=theta
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
