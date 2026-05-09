"""Additional analysis sections (win/loss, streaks, hold time, best/worst, MFE/MAE, long/short)."""

from datetime import datetime
from typing import Any


def _prepare_additional_analysis(
    inst_data: dict[str, list[dict[str, str]]],
    round_trips: list[dict[str, str]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    win_loss_data = []
    streak_data = []
    hold_time_data = []
    best_worst_trades = []
    mfe_mae_data = []
    long_short_data = []

    for inst in sorted(inst_data.keys()):
        trips = inst_data[inst]
        wins = [float(rt["pnl"]) for rt in trips if float(rt["pnl"]) > 0]
        losses = [float(rt["pnl"]) for rt in trips if float(rt["pnl"]) < 0]

        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        win_loss_ratio = avg_win / abs(avg_loss) if avg_loss != 0 else 0
        win_loss_data.append(
            {
                "instrument": inst,
                "wins": len(wins),
                "losses": len(losses),
                "win_pct": (len(wins) / len(trips)) * 100 if trips else 0,
                "avg_win": avg_win,
                "avg_loss": avg_loss,
                "ratio": win_loss_ratio,
            }
        )

        trips_sorted = sorted(
            trips, key=lambda x: datetime.fromisoformat(x["exit_ts"].replace("Z", "+00:00"))
        )
        max_win_streak = max_loss_streak = current_win_streak = current_loss_streak = 0
        for rt in trips_sorted:
            pnl = float(rt["pnl"])
            if pnl > 0:
                current_win_streak += 1
                current_loss_streak = 0
                max_win_streak = max(max_win_streak, current_win_streak)
            else:
                current_loss_streak += 1
                current_win_streak = 0
                max_loss_streak = max(max_loss_streak, current_loss_streak)
        last_pnl = float(trips_sorted[-1]["pnl"]) if trips_sorted else 0
        current_state = f"W:{current_win_streak}" if last_pnl > 0 else f"L:{current_loss_streak}"
        streak_data.append(
            {
                "instrument": inst,
                "max_win": max_win_streak,
                "max_loss": max_loss_streak,
                "current": current_state,
            }
        )

        hold_times = [float(rt.get("hold_minutes", 0)) for rt in trips]
        avg_hold = sum(hold_times) / len(hold_times) if hold_times else 0
        variance = (
            (sum((x - avg_hold) ** 2 for x in hold_times) / len(hold_times)) if hold_times else 0
        )
        hold_time_data.append(
            {
                "instrument": inst,
                "avg": avg_hold,
                "min": min(hold_times) if hold_times else 0,
                "max": max(hold_times) if hold_times else 0,
                "std": variance**0.5,
            }
        )

        best = max(trips, key=lambda x: float(x["pnl"]))
        worst = min(trips, key=lambda x: float(x["pnl"]))
        best_worst_trades.append(
            {
                "instrument": inst,
                "best": {
                    "pnl": float(best["pnl"]),
                    "entry_ts": best["entry_ts"],
                    "hold_minutes": float(best["hold_minutes"]),
                    "direction": best["direction"],
                },
                "worst": {
                    "pnl": float(worst["pnl"]),
                    "entry_ts": worst["entry_ts"],
                    "hold_minutes": float(worst["hold_minutes"]),
                    "direction": worst["direction"],
                },
            }
        )

        trips_with_mfe = [rt for rt in trips if rt.get("mfe_pnl", "") not in ("", None)]
        if trips_with_mfe:
            total_mfe_pnl = sum(float(rt["mfe_pnl"]) for rt in trips_with_mfe)
            total_mae_pnl = sum(float(rt["mae_pnl"]) for rt in trips_with_mfe)
            total_pnl = sum(float(rt["pnl"]) for rt in trips)
            mfe_mae_data.append(
                {
                    "instrument": inst,
                    "avg_mfe": total_mfe_pnl / len(trips),
                    "avg_mae": total_mae_pnl / len(trips),
                    "mfe_ratio": total_mfe_pnl / total_pnl if total_pnl != 0 else 0,
                    "mae_ratio": total_mae_pnl / total_pnl if total_pnl != 0 else 0,
                }
            )

        long_trades = [rt for rt in trips if rt.get("direction", "").lower() == "long"]
        short_trades = [rt for rt in trips if rt.get("direction", "").lower() == "short"]
        long_pnl = sum(float(rt["pnl"]) for rt in long_trades)
        short_pnl = sum(float(rt["pnl"]) for rt in short_trades)
        if long_pnl + short_pnl != 0:
            long_bias = (long_pnl / (long_pnl + short_pnl)) * 100
        else:
            long_bias = 50
        bias_str = (
            f"L{long_bias:.0f}%"
            if long_bias > 55
            else (f"S{100 - long_bias:.0f}%" if long_bias < 45 else "Neutral")
        )
        long_short_data.append(
            {
                "instrument": inst,
                "long_cnt": len(long_trades),
                "short_cnt": len(short_trades),
                "long_pnl": long_pnl,
                "short_pnl": short_pnl,
                "bias": bias_str,
            }
        )

    return (
        win_loss_data,
        streak_data,
        hold_time_data,
        best_worst_trades,
        mfe_mae_data,
        long_short_data,
    )
