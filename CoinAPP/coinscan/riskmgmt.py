"""Risk management – velikost pozice, SL a TP plán podle risk ratingu coinu."""


def plan(cfg: dict, risk_label: str | None, entry_mcap: float | None = None,
         bankroll: float | None = None) -> dict:
    r = cfg.get("risk_mgmt", {})
    bank = bankroll if bankroll is not None else float(r.get("bankroll_sol", 1))
    risk_pct = float(r.get("risk_per_trade_pct", 2)) / 100
    sl = max(float(r.get("stop_loss_pct", 35)), 1) / 100
    mult = (r.get("label_multiplier") or {}).get(risk_label or "", 0.3 if risk_label in (None, "?") else 1.0)
    max_pos = bank * float(r.get("max_position_pct", 10)) / 100

    raw = bank * risk_pct / sl                # při SL ztratíš přesně risk_pct bankrollu
    size = min(raw * mult, max_pos)
    loss_at_sl = size * sl

    tps = []
    remaining = 100.0
    for x, pct in r.get("take_profits") or []:
        pct = min(pct, remaining)
        remaining -= pct
        tps.append({"multiple": x, "sell_pct": pct,
                    "mcap": entry_mcap * x if entry_mcap else None,
                    "sol_out": size * pct / 100 * x})
    return {
        "bankroll": bank,
        "size_sol": round(size, 4),
        "size_pct": size / bank * 100 if bank else 0,
        "multiplier": mult,
        "stop_loss_pct": sl * 100,
        "sl_mcap": entry_mcap * (1 - sl) if entry_mcap else None,
        "loss_at_sl": round(loss_at_sl, 4),
        "take_profits": tps,
        "moonbag_pct": remaining,
        "max_open": r.get("max_open_positions"),
        "daily_limit_sol": bank * float(r.get("daily_loss_limit_pct", 10)) / 100,
        "capped": raw * mult > max_pos,
    }


def fmt_plan(p: dict) -> str:
    lines = [f"Pozice: <b>{p['size_sol']:.3f} SOL</b> ({p['size_pct']:.1f} % bankrollu, násobič {p['multiplier']}×)"
             + (" – omezeno stropem" if p["capped"] else ""),
             f"Stop-loss: −{p['stop_loss_pct']:.0f} %"
             + (f" (mcap ~${p['sl_mcap']:,.0f})" if p["sl_mcap"] else "")
             + f" → ztráta ~{p['loss_at_sl']:.3f} SOL"]
    for tp in p["take_profits"]:
        lines.append(f"TP {tp['multiple']:g}×: prodat {tp['sell_pct']:.0f} %"
                     + (f" (mcap ~${tp['mcap']:,.0f})" if tp["mcap"] else ""))
    if p["moonbag_pct"] > 0:
        lines.append(f"Moonbag: {p['moonbag_pct']:.0f} % nechat běžet (trailing / zbytek zdarma)")
    lines.append(f"Max otevřených pozic: {p['max_open']} | denní stop: −{p['daily_limit_sol']:.2f} SOL")
    return "<br>".join(lines)
