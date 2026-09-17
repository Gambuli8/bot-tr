"""
Modo CARRY: captura de funding delta-neutral en BingX.

Por cada par: se compra la moneda en SPOT y se abre un SHORT del mismo tamaño en el perpetuo
(margen aislado, apalancamiento CARRY_LEVERAGE). El precio se compensa entre las dos piernas y el
short cobra el funding cuando es positivo. Validado en scripts/backtest_funding.py.

Reparto del capital de cada par (L = apalancamiento del short):
  spot = capital · L/(L+1)      margen del short = capital/(L+1)

Ciclo (cada CARRY_INTERVAL_S):
  1. Contabilidad: suma al libro del par el funding, PnL realizado y comisiones de futuros (API income).
  2. Cobertura: si spot y short difieren más de lo tolerable, ajusta el short para igualar al spot.
  3. Protección (el único riesgo real es que el precio SUBA y liquide el short):
       si el precio subió ±CARRY_REBALANCE_PCT/L desde el último ajuste o la liquidación está cerca →
       primero agrega como margen el USDT acumulado (funding); si no alcanza, vende una parte del spot,
       reduce el short en la misma cantidad y pasa esos USDT a futuros.
  4. Reinversión / recentrado: si el precio bajó ±CARRY_REBALANCE_PCT/L o lo acumulado en futuros supera
     CARRY_REINVEST_PCT del capital (y pasaron 7 días) → cierra el short, rearma el reparto objetivo con
     todo el capital (incluye el funding ganado) y vuelve a abrir el short. Interés compuesto.

Nunca hace retiros: sólo transferencias internas spot ↔ futuros de la propia cuenta.
Los pares del carry quedan EXCLUIDOS de la estrategia direccional (en modo one-way se anularían).
"""

from __future__ import annotations

import logging
import threading
import time
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Callable, Optional

from bot.bingx import BingXClient, BingXError, Position
from bot.config import Settings
from bot.fmt import money, pct, price, qty as fmt_qty
from bot.narrator import Narrator, esc
from bot.store import Store

log = logging.getLogger(__name__)

SPOT_FEE = 0.001
BUY_BUFFER = 0.004            # colchón al transferir para compras a mercado
MIN_RESET_INTERVAL_MS = 7 * 24 * 3_600_000
FUTURES_ACCOUNT = "USDTMPerp"
SPOT_ACCOUNT = "spot"


def floor_step(value: float, step: Decimal) -> Decimal:
    if value <= 0:
        return Decimal(0)
    return (Decimal(str(value)) / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_step(value: float, step: Decimal) -> Decimal:
    if value <= 0:
        return Decimal(0)
    return (Decimal(str(value)) / step).to_integral_value(rounding=ROUND_UP) * step


def dec_str(d: Decimal) -> str:
    return format(d.normalize(), "f") if d != 0 else "0"


class CarryError(Exception):
    pass


class CarryManager:
    def __init__(self, settings: Settings, client: BingXClient, store: Store, narrator: Narrator,
                 notify: Callable[[str], None], sleep: Callable[[float], None] = time.sleep):
        self.s = settings
        self.client = client
        self.store = store
        self.narrator = narrator
        self.notify = notify
        self.sleep = sleep
        self.L = float(settings.carry_leverage)
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._spot_specs: dict[str, dict] = {}

    # ───────── estado ─────────

    @property
    def state(self) -> dict:
        return self.store.state.setdefault("carry", {"pairs": {}})

    @property
    def pairs(self) -> dict:
        return self.state.setdefault("pairs", {})

    def active_symbols(self) -> list[str]:
        return [s for s, p in self.pairs.items() if p.get("status") == "active"]

    def should_run(self) -> bool:
        return self.s.carry_enabled or bool(self.active_symbols())

    # ───────── ciclo ─────────

    def start(self) -> None:
        if not self.should_run():
            return
        threading.Thread(target=self._loop, name="carry", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("Error en ciclo carry")
            self._stop.wait(self.s.carry_interval_s)

    @property
    def paper(self) -> bool:
        return bool(getattr(self.client, "paper", False))

    def run_once(self) -> None:
        with self.lock:
            if hasattr(self.client, "tick"):
                self.client.tick()          # simulador: aplica funding real y liquidaciones
            positions = {p.symbol: p for p in self.client.positions()}
            balances = self.client.spot_balances()
            for symbol in self.s.carry_symbols:
                pair = self.pairs.get(symbol)
                try:
                    if pair and pair.get("status") == "active":
                        self._manage(symbol, pair, positions.get(symbol), balances)
                    elif self.s.carry_enabled and (
                            not pair or pair.get("status") == "pending"
                            or (pair.get("status") == "error" and time.time() * 1000 >= pair.get("retry_after", 0))):
                        self._build(symbol, positions.get(symbol), balances)
                except (BingXError, CarryError) as exc:
                    log.error("Carry %s: %s", symbol, exc)
                    self.store.log_event("carry_error", symbol=symbol, error=str(exc))
                    self._alert_once(f"err:{symbol}:{str(exc)[:60]}",
                                     f"Carry {symbol}: {exc}", critical=isinstance(exc, CarryError))
            self.state["last_run"] = int(time.time() * 1000)
            self.store.save()

    # ───────── utilidades ─────────

    def _head(self, emoji: str, title: str) -> str:
        label = "CARRY SIMULADO 🧪" if self.paper else self.s.mode_label
        return f"{emoji} <b>{esc(title)}</b> · <i>{label}</i>"

    def _alert_once(self, key: str, text: str, critical: bool = False) -> None:
        alerts = self.store.state.setdefault("alerts", {})
        now = time.time()
        if now - alerts.get(key, 0) < 6 * 3600:
            return
        alerts[key] = now
        self.notify(self.narrator.alert(text, critical=critical))

    def _coin(self, symbol: str) -> str:
        return symbol.split("-")[0]

    def _spot_spec(self, symbol: str) -> dict:
        if not self._spot_specs:
            self._spot_specs = self.client.spot_symbols()
        spec = self._spot_specs.get(symbol)
        if not spec:
            raise CarryError(f"{symbol} no tiene mercado spot en BingX")
        return spec

    def _spot_step(self, symbol: str) -> Decimal:
        return Decimal(str(self._spot_spec(symbol).get("stepSize") or "0.000001"))

    def _buy_qty_for(self, symbol: str, coins_needed: float) -> Decimal:
        """Cantidad a comprar para RECIBIR `coins_needed` (BingX descuenta la comisión en la moneda)."""
        return ceil_step(coins_needed / (1 - SPOT_FEE), self._spot_step(symbol))

    def _perp_step(self, symbol: str) -> Decimal:
        return Decimal(1).scaleb(-self.client.contract(symbol).qty_precision)

    def _asset(self) -> str:
        if self.paper:
            return "USDT"
        return self.state.get("asset") or self.s.carry_asset or ("VST" if not self.s.is_live else "USDT")

    def _fail(self, symbol: str, reason: str) -> CarryError:
        """Marca el par con error y lo reintenta recién en 1 hora (nunca en cada ciclo)."""
        self.pairs[symbol] = {"status": "error", "reason": reason,
                              "retry_after": int(time.time() * 1000) + 3_600_000}
        return CarryError(reason)

    def _refund_spot(self, max_amount: float) -> float:
        """Devuelve a futuros el saldo que quedó en spot tras una operación fallida."""
        available = self.client.spot_balances().get(self._asset(), 0.0)
        amount = min(max_amount, available)
        try:
            return self._transfer(amount, SPOT_ACCOUNT, FUTURES_ACCOUNT) if amount >= 0.01 else 0.0
        except (BingXError, CarryError) as exc:
            log.error("No pude devolver %.2f a futuros: %s", amount, exc)
            return 0.0

    def _transfer(self, amount: float, from_acc: str, to_acc: str) -> float:
        amount = float(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))
        if amount < 0.01:
            return 0.0
        asset = self._asset()
        try:
            self.client.transfer(asset, amount, from_acc, to_acc)
        except BingXError as exc:
            alt = "USDT" if asset == "VST" else "VST"
            if self.s.carry_asset or self.state.get("asset"):
                raise CarryError(f"no pude transferir {amount} {asset} de {from_acc} a {to_acc}: {exc}")
            log.warning("Transferencia con %s falló (%s); pruebo %s", asset, exc, alt)
            self.client.transfer(alt, amount, from_acc, to_acc)
            asset = alt
        self.state["asset"] = asset
        self.store.log_event("carry_transfer", amount=amount, asset=asset, frm=from_acc, to=to_acc)
        return amount

    def _spot_trade(self, symbol: str, side: str, qty: Decimal) -> tuple[float, float]:
        """Devuelve (monedas netas recibidas o vendidas, USDT gastados o cobrados netos)."""
        coin = self._coin(symbol)
        before = self.client.spot_balances().get(coin, 0.0)
        order = self.client.spot_market_order(symbol, side, dec_str(qty))
        self.sleep(1.0)
        after = self.client.spot_balances().get(coin, 0.0)
        quote = float(order.get("cummulativeQuoteQty") or 0) or float(qty) * self.client.price(symbol)
        if side == "BUY":
            received = after - before if after > before else float(qty) * (1 - SPOT_FEE)
            return received, quote
        sold = before - after if before > after else float(qty)
        return sold, quote * (1 - SPOT_FEE)

    def _sync_income(self, symbol: str, pair: dict) -> None:
        cursor = int(pair.get("income_cursor", 0))
        rows = self.client.income(symbol, cursor + 1)
        latest = cursor
        for r in rows:
            t = int(r.get("time", 0) or 0)
            if t <= cursor:
                continue
            amount = float(r.get("income", 0) or 0)
            kind = r.get("incomeType")
            if kind == "FUNDING_FEE":
                pair["funding"] = pair.get("funding", 0.0) + amount
            elif kind == "TRADING_FEE":
                pair["fees"] = pair.get("fees", 0.0) - amount
            elif kind == "REALIZED_PNL":
                pair["realized"] = pair.get("realized", 0.0) + amount
            else:
                continue
            pair["fut_cash"] = pair.get("fut_cash", 0.0) + amount
            latest = max(latest, t)
        pair["income_cursor"] = latest

    def _wait_short(self, symbol: str, min_qty: float = 0.0, timeout: float = 8.0) -> Optional[Position]:
        deadline = time.time() + timeout
        while True:
            for p in self.client.positions(symbol):
                if p.symbol == symbol and p.side == "SHORT" and p.qty >= min_qty:
                    return p
            if time.time() >= deadline:
                return None
            self.sleep(1.0)

    def _open_short(self, symbol: str, qty: Decimal) -> None:
        self.client.set_margin_type(symbol, "ISOLATED")
        self.client.set_leverage(symbol, int(self.L), "BOTH")
        self.client.place_market(symbol, "SELL", dec_str(qty))

    # ───────── armado ─────────

    def _build(self, symbol: str, position: Optional[Position], balances: dict) -> None:
        if position is not None:
            self._alert_once(f"wait:{symbol}", f"Carry {symbol}: hay una operación abierta en ese par; "
                                               f"armo el carry cuando se cierre.")
            self.pairs[symbol] = {"status": "pending", "reason": "operación direccional abierta"}
            return

        capital = self.s.carry_capital_usdt / max(1, len(self.s.carry_symbols))
        perp = self.client.contract(symbol)
        spot = self._spot_spec(symbol)
        perp_step = self._perp_step(symbol)
        px = self.client.price(symbol)

        spot_budget = capital * self.L / (self.L + 1)
        qty = floor_step(spot_budget / (px * (1 + SPOT_FEE + BUY_BUFFER)), perp_step)   # tamaño del short
        min_notional = max(perp.min_usdt, float(spot.get("minNotional") or 0),
                           perp.min_qty * px, float(spot.get("minQty") or 0) * px)
        if float(qty) * px < min_notional or float(qty) < perp.min_qty:
            need = min_notional * (self.L + 1) / self.L * 1.02 * len(self.s.carry_symbols)
            raise self._fail(symbol, f"con {money(capital)} por par no llego al mínimo de {symbol} "
                                     f"(hace falta CARRY_CAPITAL_USDT ≥ {money(need)})")

        cursor = int(time.time() * 1000)
        buy_qty = self._buy_qty_for(symbol, float(qty))
        transfer_amt = float(buy_qty) * px * (1 + BUY_BUFFER)
        sent = self._transfer(transfer_amt, FUTURES_ACCOUNT, SPOT_ACCOUNT)
        try:
            received, spent = self._spot_trade(symbol, "BUY", buy_qty)
        except (BingXError, CarryError) as exc:
            refunded = self._refund_spot(sent)
            raise self._fail(symbol, f"falló la compra spot de {symbol} ({exc}); devolví {money(refunded)} a futuros")

        short_qty = min(qty, floor_step(received, perp_step))
        try:
            self._open_short(symbol, short_qty)
            pos = self._wait_short(symbol, float(short_qty) * 0.99)
            if pos is None:
                raise CarryError("no veo el short abierto")
        except (BingXError, CarryError) as exc:
            # Nunca dejar el spot sin cubrir: se deshace la compra.
            self._spot_trade(symbol, "SELL", floor_step(received, self._spot_step(symbol)))
            self._refund_spot(sent * 1.01)
            raise self._fail(symbol, f"falló el short de {symbol} ({exc}); vendí el spot comprado para no quedar "
                                     f"expuesto y devolví los fondos a futuros")

        leftover = sent - spent
        returned = self._transfer(leftover, SPOT_ACCOUNT, FUTURES_ACCOUNT) if leftover >= 1 else 0.0
        self.pairs[symbol] = {
            "status": "active", "capital": capital, "opened_at": cursor, "anchor": px,
            "spot_qty": received, "short_qty": float(short_qty),
            "fut_cash": capital - sent + returned, "spot_cash": leftover - returned,
            "funding": 0.0, "fees": spent * SPOT_FEE, "realized": 0.0,
            "income_cursor": cursor - 1, "last_reset": cursor, "resets": 0, "protections": 0,
        }
        self.store.log_event("carry_open", symbol=symbol, spot_qty=received, short_qty=float(short_qty), price=px)
        self.notify(
            f"{self._head('🧲', f'Carry armado en {self._coin(symbol)}')}\n\n"
            f"🟢 Spot: compré {fmt_qty(received)} {self._coin(symbol)} a ~{price(px)} ({money(spent)})\n"
            f"🔴 Short ×{self.L:g}: {fmt_qty(float(short_qty))} {self._coin(symbol)} (misma cantidad → el precio se compensa)\n"
            f"💰 Capital del par: {money(capital)}\n\n"
            f"👉 A partir de ahora cobro el funding cada 8 h mientras sea positivo.")

    # ───────── gestión ─────────

    def _manage(self, symbol: str, pair: dict, pos: Optional[Position], balances: dict) -> None:
        self._sync_income(symbol, pair)
        px = self.client.price(symbol)
        coin = self._coin(symbol)
        spot_actual = balances.get(coin, 0.0)

        if spot_actual < pair["spot_qty"] * 0.98:
            self._alert_once(f"spot:{symbol}", f"Carry {symbol}: en spot hay {fmt_qty(spot_actual)} {coin} y el "
                                               f"carry esperaba {fmt_qty(pair['spot_qty'])}. Ajusto al saldo real.",
                             critical=True)
            pair["spot_qty"] = spot_actual

        if pos is None or pos.side != "SHORT":
            if pair["spot_qty"] * px >= 2:
                self._alert_once(f"noshort:{symbol}", f"Carry {symbol}: el short no está abierto "
                                                      f"(¿liquidado o cerrado a mano?). Lo vuelvo a abrir.", critical=True)
                self._open_short(symbol, floor_step(pair["spot_qty"], self._perp_step(symbol)))
                pos = self._wait_short(symbol)
                pair["anchor"] = px
            if pos is None:
                return

        # Cobertura: el short tiene que igualar al spot
        diff = pair["spot_qty"] - pos.qty
        if abs(diff) * px >= max(2.0, 0.03 * pair["spot_qty"] * px):
            d = floor_step(abs(diff), self._perp_step(symbol))
            if d > 0:
                if diff > 0:
                    self.client.place_market(symbol, "SELL", dec_str(d))
                else:
                    self.client.place_market(symbol, "BUY", dec_str(d), reduce_only=True)
                self.store.log_event("carry_hedge_fix", symbol=symbol, diff=diff)
                self.notify(self.narrator.alert(f"Carry {symbol}: la cobertura estaba desbalanceada "
                                                f"({fmt_qty(diff)} {coin}); ajusté el short."))
                pos = self._wait_short(symbol) or pos

        fut_eq = pair["fut_cash"] + pos.unrealized_pnl
        equity = fut_eq + pair["spot_qty"] * px + pair.get("spot_cash", 0.0)
        target_fut = equity / (self.L + 1)
        band = self.s.carry_rebalance_pct / self.L
        liq_dist = (pos.liquidation_price - px) / px if pos.liquidation_price > 0 else None
        pair.update(last_price=px, equity=equity, liq_price=pos.liquidation_price, short_qty=pos.qty,
                    unrealized=pos.unrealized_pnl)

        if px >= pair["anchor"] * (1 + band) or (liq_dist is not None and liq_dist < 0.5 / self.L):
            self._protect(symbol, pair, pos, px, equity, target_fut)
        elif px <= pair["anchor"] * (1 - band) or (
                fut_eq - target_fut >= max(self.s.carry_min_trade_usdt, self.s.carry_reinvest_pct * equity)
                and time.time() * 1000 - pair.get("last_reset", 0) >= MIN_RESET_INTERVAL_MS):
            # Sólo vale la pena cerrar/reabrir si el tamaño objetivo cambia al menos un paso del contrato.
            perp_step = self._perp_step(symbol)
            target_qty = floor_step(equity * self.L / (self.L + 1) / (px * (1 + SPOT_FEE + BUY_BUFFER)), perp_step)
            if abs(float(target_qty) - pos.qty) >= float(perp_step):
                self._reset(symbol, pair, pos, px)
            else:
                pair["anchor"] = px

    def _protect(self, symbol: str, pair: dict, pos: Position, px: float, equity: float, target_fut: float) -> None:
        coin = self._coin(symbol)
        iso_eq = pos.margin + pos.unrealized_pnl if pos.margin else None
        need = target_fut - (iso_eq if iso_eq is not None else target_fut * 0.5)
        added = 0.0
        buffer = pair["fut_cash"] - (pos.margin or 0.0)
        if need > 0 and buffer >= 1:
            amount = min(need, buffer)
            try:
                self.client.add_isolated_margin(symbol, amount)
                added = amount
            except BingXError as exc:
                log.warning("Carry %s: no pude sumar margen desde el colchón: %s", symbol, exc)

        sold_qty = 0.0
        if need - added >= self.s.carry_min_trade_usdt:
            target_qty = equity * self.L / (self.L + 1) / px
            delta = floor_step(pair["spot_qty"] - target_qty, self._perp_step(symbol))
            if delta > 0 and float(delta) * px >= self.s.carry_min_trade_usdt:
                self.client.place_market(symbol, "BUY", dec_str(delta), reduce_only=True)
                sold, proceeds = self._spot_trade(symbol, "SELL", delta)
                moved = self._transfer(proceeds, SPOT_ACCOUNT, FUTURES_ACCOUNT)
                pair["fut_cash"] += moved
                pair["spot_cash"] = pair.get("spot_cash", 0.0) + proceeds - moved
                pair["spot_qty"] -= sold
                pair["fees"] = pair.get("fees", 0.0) + proceeds * SPOT_FEE
                sold_qty = sold
                try:
                    self.client.add_isolated_margin(symbol, min(need - added, moved))
                    added += min(need - added, moved)
                except BingXError as exc:
                    log.warning("Carry %s: no pude sumar margen tras vender spot: %s", symbol, exc)

        pair["anchor"] = px
        pair["protections"] = pair.get("protections", 0) + 1
        self.store.log_event("carry_protect", symbol=symbol, price=px, margin_added=added, spot_sold=sold_qty)
        detail = f"💵 Sumé {money(added)} de margen al short" + (
            f" y vendí {fmt_qty(sold_qty)} {coin} de spot (reduje el short igual)" if sold_qty else " con el funding acumulado")
        self.notify(f"{self._head('🛡️', f'Carry {coin}: protección por suba de precio')}\n\n"
                    f"📈 El precio subió a {price(px)}.\n{detail}.\n"
                    f"🧮 La posición sigue cubierta (spot = short).")

    def _reset(self, symbol: str, pair: dict, pos: Position, px: float) -> None:
        coin = self._coin(symbol)
        perp_step = self._perp_step(symbol)
        self.client.place_market(symbol, "BUY", dec_str(Decimal(str(pos.qty))), reduce_only=True)
        self.sleep(3.0)
        self._sync_income(symbol, pair)
        equity = pair["fut_cash"] + pair["spot_qty"] * px + pair.get("spot_cash", 0.0)
        target_qty = floor_step(equity * self.L / (self.L + 1) / (px * (1 + SPOT_FEE + BUY_BUFFER)), perp_step)
        before_qty = pair["spot_qty"]

        try:
            if float(target_qty) - pair["spot_qty"] >= float(perp_step) and \
                    (float(target_qty) - pair["spot_qty"]) * px >= self.s.carry_min_trade_usdt:
                buy_qty = self._buy_qty_for(symbol, float(target_qty) - pair["spot_qty"])
                sent = self._transfer(float(buy_qty) * px * (1 + BUY_BUFFER), FUTURES_ACCOUNT, SPOT_ACCOUNT)
                received, spent = self._spot_trade(symbol, "BUY", buy_qty)
                pair["fut_cash"] -= sent
                pair["spot_cash"] = pair.get("spot_cash", 0.0) + sent - spent
                pair["spot_qty"] += received
                pair["fees"] = pair.get("fees", 0.0) + spent * SPOT_FEE
            elif pair["spot_qty"] - float(target_qty) >= float(perp_step) and \
                    (pair["spot_qty"] - float(target_qty)) * px >= self.s.carry_min_trade_usdt:
                delta = floor_step(pair["spot_qty"] - float(target_qty), self._spot_step(symbol))
                sold, proceeds = self._spot_trade(symbol, "SELL", delta)
                pair["spot_qty"] -= sold
                pair["spot_cash"] = pair.get("spot_cash", 0.0) + proceeds
                pair["fees"] = pair.get("fees", 0.0) + proceeds * SPOT_FEE
            if pair.get("spot_cash", 0.0) >= 1:
                moved = self._transfer(pair["spot_cash"], SPOT_ACCOUNT, FUTURES_ACCOUNT)
                pair["spot_cash"] -= moved
                pair["fut_cash"] += moved
        finally:
            # Pase lo que pase con el spot, el short se vuelve a abrir por lo que haya en spot.
            reopen = min(target_qty, floor_step(pair["spot_qty"], perp_step)) if target_qty > 0 \
                else floor_step(pair["spot_qty"], perp_step)
            self._open_short(symbol, reopen)
            new_pos = self._wait_short(symbol)
            if new_pos is None:
                raise CarryError(f"no pude reabrir el short de {symbol}: el spot quedó SIN cubrir")

        pair["anchor"] = px
        pair["last_reset"] = int(time.time() * 1000)
        pair["resets"] = pair.get("resets", 0) + 1
        self.store.log_event("carry_reset", symbol=symbol, price=px, equity=equity,
                             spot_before=before_qty, spot_after=pair["spot_qty"])
        change = pair["spot_qty"] - before_qty
        self.notify(f"{self._head('🔁', f'Carry {coin}: reinversión')}\n\n"
                    f"🧮 Capital del par: {money(equity)} (inicial {money(pair['capital'])})\n"
                    f"🟢 Spot: {fmt_qty(before_qty)} → {fmt_qty(pair['spot_qty'])} {coin} "
                    f"({'+' if change >= 0 else ''}{fmt_qty(change)})\n"
                    f"🔴 Short reabierto por la misma cantidad a {price(px)}\n"
                    f"💸 Funding cobrado hasta ahora: {money(pair.get('funding', 0.0), True)}")

    # ───────── cierre ─────────

    def unwind(self, symbol: str) -> str:
        with self.lock:
            pair = self.pairs.get(symbol)
            if not pair or pair.get("status") != "active":
                return f"{symbol}: no hay carry activo."
            coin = self._coin(symbol)
            for p in self.client.positions(symbol):
                if p.symbol == symbol and p.side == "SHORT":
                    self.client.place_market(symbol, "BUY", dec_str(Decimal(str(p.qty))), reduce_only=True)
            spot_qty = floor_step(min(pair["spot_qty"], self.client.spot_balances().get(coin, 0.0)),
                                  self._spot_step(symbol))
            proceeds = 0.0
            if spot_qty > 0:
                _, proceeds = self._spot_trade(symbol, "SELL", spot_qty)
            self._transfer(proceeds + pair.get("spot_cash", 0.0), SPOT_ACCOUNT, FUTURES_ACCOUNT)
            self.sleep(3.0)
            self._sync_income(symbol, pair)
            pair["status"] = "closed"
            pair["closed_at"] = int(time.time() * 1000)
            self.store.log_event("carry_close", symbol=symbol, funding=pair.get("funding", 0.0))
            self.store.save()
            return (f"Carry {coin} cerrado: short cerrado, vendí {dec_str(spot_qty)} {coin} de spot y pasé los USDT "
                    f"a futuros. Funding cobrado: {money(pair.get('funding', 0.0), True)}.")

    # ───────── /carry ─────────

    def status_text(self) -> str:
        pairs = {s: p for s, p in self.pairs.items() if p.get("status") in ("active", "pending", "error")}
        label = "SIMULADO 🧪 (precios y funding reales, órdenes simuladas)" if self.paper else self.s.mode_label
        lines = [f"🧲 <b>Carry (captura de funding)</b> · <i>{label}</i>",
                 f"⚙️ {'activado' if self.s.carry_enabled else 'desactivado (sólo gestiono lo abierto)'} · "
                 f"short ×{self.L:g} · capital {money(self.s.carry_capital_usdt)}"]
        if not pairs:
            lines.append("\n💤 Sin pares armados todavía.")
            return "\n".join(lines)
        total_cap = total_eq = total_funding = total_fees = 0.0
        for symbol, p in pairs.items():
            coin = self._coin(symbol)
            if p.get("status") != "active":
                lines.append(f"\n🪙 <b>{coin}</b> — ⏳ {esc(p.get('status'))}: {esc(p.get('reason', ''))}")
                continue
            cap, eq = p.get("capital", 0.0), p.get("equity", p.get("capital", 0.0))
            total_cap += cap
            total_eq += eq
            total_funding += p.get("funding", 0.0)
            total_fees += p.get("fees", 0.0)
            liq = p.get("liq_price")
            px = p.get("last_price")
            liq_txt = f" · liquidación {price(liq)} ({pct((liq - px) / px * 100, True)})" if liq and px else ""
            lines += [f"\n🪙 <b>{coin}</b> · {price(px)}",
                      f"   🟢 Spot {fmt_qty(p['spot_qty'])} · 🔴 Short {fmt_qty(p.get('short_qty', 0))}{liq_txt}",
                      f"   💸 Funding {money(p.get('funding', 0.0), True)} · 🧾 Comisiones {money(-p.get('fees', 0.0), True)}",
                      f"   🧮 Capital {money(eq)} ({money(eq - cap, True)})"]
        days = max(1e-9, (time.time() * 1000 - min(p.get("opened_at", time.time() * 1000)
                                                   for p in pairs.values() if p.get("status") == "active")) / 86_400_000) \
            if any(p.get("status") == "active" for p in pairs.values()) else 0
        lines += ["", f"📊 <b>Total</b>: capital {money(total_eq)} · resultado {money(total_eq - total_cap, True)}",
                  f"💸 Funding {money(total_funding, True)} · 🧾 Comisiones {money(-total_fees, True)}"]
        if days >= 7 and total_cap > 0:
            apy = (total_eq / total_cap) ** (365 / days) - 1
            lines.append(f"📈 Rendimiento anualizado: {pct(apy * 100, True)} ({days:.0f} días)")
        return "\n".join(lines)
