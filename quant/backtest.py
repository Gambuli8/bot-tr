"""Motor de backtest vela a vela, con costos reales y la misma gestión de riesgo que el bot.

Supuestos de ejecución (conservadores):
- Señal al CIERRE de la vela t → orden a mercado en la APERTURA de t+1, con slippage y comisión.
- Stop: si el mínimo de la vela toca el stop, se ejecuta al stop; si la vela ABRE por debajo (gap), a la apertura.
  En ambos casos, menos slippage y comisión.
- Si en la misma vela se entra y se toca el stop, se asume que el stop ocurrió (peor caso).
- Funding: se cobra/paga cada vela según FUNDING_RATE_8H (0 en spot, explícito).
- Kill-switch diario y halt por drawdown evaluados al cierre de cada vela con el equity marcado a mercado.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from quant import config as C
from quant.risk import RiskManager, RiskState, position_size


@dataclass
class Costs:
    fee: float = C.FEE_RATE
    slippage: float = C.SLIPPAGE_RATE
    funding_8h: float = C.FUNDING_RATE_8H

    def scaled(self, k: float) -> "Costs":
        return Costs(self.fee * k, self.slippage * k, self.funding_8h * k)


@dataclass
class Result:
    equity: pd.Series
    trades: pd.DataFrame
    stats: dict = field(default_factory=dict)
    risk_events: list = field(default_factory=list)


def run(df: pd.DataFrame, sig: pd.DataFrame, costs: Costs = Costs(), capital: float = C.INITIAL_CAPITAL_USDT,
        start: int = 0, end: int | None = None, apply_halt: bool = False) -> Result:
    """Backtest sobre df.iloc[start:end]. `sig` debe estar alineado con df (calculado sobre la serie completa,
    así los indicadores tienen historia previa sin mirar al futuro)."""
    end = len(df) if end is None else end
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    ts = df["ts"].to_numpy()
    days = pd.to_datetime(ts, unit="ms", utc=True).strftime("%Y-%m-%d").to_numpy()
    entry_sig, exit_sig = sig["entry"].to_numpy(bool), sig["exit"].to_numpy(bool)
    stop_dist, trail = sig["stop_dist"].to_numpy(float), sig["trail"].to_numpy(float)
    fund_per_bar = costs.funding_8h * (C.BAR_MS / (8 * 3600 * 1000))

    cash, qty, stop = capital, 0.0, 0.0
    entry_px = entry_fee = entry_eq = init_risk = 0.0
    entry_i = -1
    pend_entry = pend_exit = False
    risk = RiskManager(RiskState(peak_equity=capital, day_start_equity=capital))
    eq = np.empty(end - start)
    in_pos = np.zeros(end - start, bool)
    trades: list[dict] = []
    fees_total = funding_total = 0.0

    def close_pos(i: int, px_raw: float, reason: str) -> None:
        nonlocal cash, qty, fees_total
        px = px_raw * (1 - costs.slippage)
        fee = qty * px * costs.fee
        cash += qty * px - fee
        fees_total += fee
        pnl = qty * (px - entry_px) - entry_fee - fee
        trades.append({"entry_ts": ts[entry_i], "exit_ts": ts[i], "entry_px": entry_px, "exit_px": px, "qty": qty,
                       "pnl": pnl, "ret": pnl / entry_eq, "r": pnl / init_risk if init_risk else np.nan,
                       "bars": i - entry_i + 1, "reason": reason})
        qty = 0.0

    for k, i in enumerate(range(start, end)):
        risk.on_new_day(days[i], cash + qty * o[i])
        # 1) órdenes a la apertura (decididas al cierre anterior)
        if pend_exit and qty > 0:
            close_pos(i, o[i], "signal_exit")
        pend_exit = False
        if pend_entry and qty == 0:
            ok, _ = risk.can_enter()
            if ok and (not apply_halt or not risk.s.halted):
                px = o[i] * (1 + costs.slippage)
                stop_lvl = px - stop_dist[i - 1]
                d = position_size(cash, px, stop_lvl)
                if d.ok:
                    qty, entry_px, stop, entry_i, entry_eq = d.qty, px, stop_lvl, i, cash
                    entry_fee = qty * px * costs.fee
                    cash -= qty * px + entry_fee
                    fees_total += entry_fee
                    init_risk = qty * (px - stop_lvl)
        pend_entry = False
        # 2) stop intrabar (trailing calculado al cierre anterior)
        if qty > 0:
            if i > entry_i and not np.isnan(trail[i - 1]):
                stop = max(stop, trail[i - 1])
            if l[i] <= stop:
                close_pos(i, min(o[i], stop), "stop")
        # 3) funding y marcado a mercado
        if qty > 0 and fund_per_bar:
            f = qty * c[i] * fund_per_bar
            cash -= f
            funding_total += f
        equity = cash + qty * c[i]
        action = risk.check(equity, str(days[i]))
        if action and qty > 0 and (action == "flatten_day" or apply_halt):
            close_pos(i, c[i], action)
            equity = cash
        # 4) señales al cierre
        if i < end - 1:
            if qty == 0 and entry_sig[i]:
                pend_entry = True
            elif qty > 0 and exit_sig[i]:
                pend_exit = True
        else:
            if qty > 0:
                close_pos(i, c[i], "end_of_test")
                equity = cash
        eq[k] = equity
        in_pos[k] = qty > 0

    equity_s = pd.Series(eq, index=pd.to_datetime(ts[start:end], unit="ms", utc=True))
    tr = pd.DataFrame(trades)
    res = Result(equity_s, tr, risk_events=risk.s.events)
    res.stats = compute_stats(equity_s, tr, capital, in_pos.mean() if len(in_pos) else 0.0)
    res.stats.update(fees=fees_total, funding=funding_total)
    return res


def max_drawdown(equity: pd.Series | np.ndarray) -> float:
    e = np.asarray(equity, float)
    if len(e) == 0:
        return 0.0
    peak = np.maximum.accumulate(e)
    return float(np.max(1 - e / peak))


def compute_stats(equity: pd.Series, trades: pd.DataFrame, capital: float, exposure: float = 1.0) -> dict:
    n_bars = len(equity)
    final = float(equity.iloc[-1]) if n_bars else capital
    net = final / capital - 1
    years = n_bars / C.BARS_PER_YEAR
    cagr = (final / capital) ** (1 / years) - 1 if years > 0 and final > 0 else -1.0
    rets = equity.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(C.BARS_PER_YEAR)) if len(rets) > 1 and rets.std() > 0 else 0.0
    mdd = max_drawdown(equity)
    n = len(trades)
    if n:
        wins, losses = trades.loc[trades.pnl > 0, "pnl"], trades.loc[trades.pnl <= 0, "pnl"]
        pf = float(wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf")
        win_rate = float((trades.pnl > 0).mean())
        avg_ret = float(trades.ret.mean())
        t_stat = float(trades.ret.mean() / trades.ret.std() * np.sqrt(n)) if n > 1 and trades.ret.std() > 0 else 0.0
        avg_r = float(trades.r.mean())
    else:
        pf = win_rate = avg_ret = t_stat = avg_r = 0.0
    return {"net_return": net, "cagr": cagr, "sharpe": sharpe, "max_dd": mdd, "mar": cagr / mdd if mdd > 0 else 0.0,
            "trades": n, "win_rate": win_rate, "profit_factor": pf, "avg_trade_ret": avg_ret, "avg_r": avg_r,
            "t_stat": t_stat, "exposure": float(exposure), "final_equity": final, "years": years}


def buy_and_hold(df: pd.DataFrame, costs: Costs = Costs(), capital: float = C.INITIAL_CAPITAL_USDT,
                 start: int = 0, end: int | None = None) -> dict:
    end = len(df) if end is None else end
    px_in = df["open"].iloc[start] * (1 + costs.slippage)
    qty = capital / (px_in * (1 + costs.fee))
    closes = df["close"].iloc[start:end].to_numpy(float)
    eq = qty * closes
    eq[-1] = qty * closes[-1] * (1 - costs.slippage) * (1 - costs.fee)
    s = pd.Series(eq, index=pd.to_datetime(df["ts"].iloc[start:end], unit="ms", utc=True))
    return compute_stats(s, pd.DataFrame(), capital) | {"equity": s}
