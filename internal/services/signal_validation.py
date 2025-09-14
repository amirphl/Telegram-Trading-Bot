from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9]{1,14}$")
EXPECTED_KEYS = {
    "is_signal", "token", "position_type", "entry_price", "leverage",
    "stop_losses", "take_profits", "confidence",
}
ACTIVE_POSITION_STATUSES = (
    "claimed", "submitting", "unknown_requires_reconciliation", "submitted",
    "open", "partially_filled", "reconciled_entry_found",
)


@dataclass(frozen=True)
class SignalValidation:
    is_signal: bool
    valid: bool
    executable: bool
    normalized: dict[str, Any] | None
    errors: tuple[str, ...]
    execution_blockers: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "is_signal": self.is_signal,
            "valid": self.valid,
            "executable": self.executable,
            "errors": list(self.errors),
            "execution_blockers": list(self.execution_blockers),
        }


def _finite_number(value: Any, field: str, errors: list[str], *, positive: bool = True) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{field}_must_be_number")
        return None
    number = float(value)
    if not math.isfinite(number):
        errors.append(f"{field}_must_be_finite")
    elif positive and number <= 0:
        errors.append(f"{field}_must_be_positive")
    return number


def _number_list(value: Any, field: str, errors: list[str]) -> list[float]:
    if not isinstance(value, list):
        errors.append(f"{field}_must_be_array")
        return []
    result: list[float] = []
    for item in value:
        number = _finite_number(item, field, errors)
        if number is not None:
            result.append(number)
    if result != sorted(result) or len(result) != len(set(result)):
        errors.append(f"{field}_must_be_strictly_ascending")
    return result


def _parse_message_date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def validate_signal_output(
    result: Any,
    cfg,
    *,
    message_date: Any,
    conn=None,
    historical: bool = False,
    allow_execution: bool = True,
    now: datetime | None = None,
) -> SignalValidation:
    errors: list[str] = []
    blockers: list[str] = []
    if not isinstance(result, dict):
        return SignalValidation(False, False, False, None, ("output_must_be_object",), ())
    missing = EXPECTED_KEYS - set(result)
    extra = set(result) - EXPECTED_KEYS
    errors.extend(f"missing_field:{key}" for key in sorted(missing))
    errors.extend(f"unexpected_field:{key}" for key in sorted(extra))
    if type(result.get("is_signal")) is not bool:
        errors.append("is_signal_must_be_boolean")
        is_signal = False
    else:
        is_signal = result["is_signal"]

    confidence = _finite_number(result.get("confidence"), "confidence", errors, positive=False)
    if confidence is not None and not 0 <= confidence <= 1:
        errors.append("confidence_out_of_range")

    if not is_signal:
        for key in ("token", "position_type", "entry_price", "leverage"):
            if result.get(key) is not None:
                errors.append(f"non_signal_{key}_must_be_null")
        for key in ("stop_losses", "take_profits"):
            if result.get(key) != []:
                errors.append(f"non_signal_{key}_must_be_empty")
        return SignalValidation(False, not errors, False, None, tuple(errors), ())

    token = result.get("token")
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        errors.append("invalid_token_syntax")
    position_type = result.get("position_type")
    if position_type not in {"long", "short"}:
        errors.append("invalid_position_type")
    entry = _finite_number(result.get("entry_price"), "entry_price", errors)
    leverage = _finite_number(result.get("leverage"), "leverage", errors)
    stops = _number_list(result.get("stop_losses"), "stop_losses", errors)
    targets = _number_list(result.get("take_profits"), "take_profits", errors)
    if not stops:
        errors.append("stop_loss_required")
    if not targets:
        errors.append("take_profit_required")
    max_leverage = float(getattr(cfg, "max_leverage", 1.0))
    if leverage is not None and leverage > max_leverage:
        errors.append("leverage_exceeds_limit")
    if entry is not None and position_type == "long":
        if any(price >= entry for price in stops):
            errors.append("long_stops_must_be_below_entry")
        if any(price <= entry for price in targets):
            errors.append("long_targets_must_be_above_entry")
    if entry is not None and position_type == "short":
        if any(price <= entry for price in stops):
            errors.append("short_stops_must_be_above_entry")
        if any(price >= entry for price in targets):
            errors.append("short_targets_must_be_below_entry")

    normalized = None
    if not errors:
        normalized = {
            "token": token,
            "position_type": position_type,
            "entry_price": entry,
            "leverage": leverage,
            "stop_losses": stops,
            "take_profits": targets,
            "confidence": confidence,
        }

    allowlist = set(getattr(cfg, "signal_token_allowlist", ()))
    if token not in allowlist:
        blockers.append("token_not_allowlisted")
    if confidence is not None and confidence < float(getattr(cfg, "signal_min_confidence", 0.75)):
        blockers.append("confidence_below_threshold")
    parsed_date = _parse_message_date(message_date)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    max_age = int(getattr(cfg, "signal_max_age_secs", 300))
    if parsed_date is None:
        blockers.append("message_date_invalid")
    elif (current - parsed_date).total_seconds() < -30:
        blockers.append("message_date_in_future")
    elif (current - parsed_date).total_seconds() > max_age:
        blockers.append("signal_too_old")
    if historical:
        blockers.append("historical_signal_non_executable")
    if not allow_execution:
        blockers.append("execution_not_authorized_for_job")
    if getattr(cfg, "signal_approval_mode", "manual") != "automatic":
        blockers.append("manual_approval_required")
    if not getattr(cfg, "enable_auto_execution", False):
        blockers.append("automatic_execution_disabled")

    if conn is not None:
        placeholders = ",".join("?" for _ in ACTIVE_POSITION_STATUSES)
        configured_notional = float(getattr(cfg, "order_notional", 0))
        row = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(COALESCE(cost, ?)), 0) FROM positions_submitted WHERE status IN ({placeholders})",
            (configured_notional, *ACTIVE_POSITION_STATUSES),
        ).fetchone()
        count, notional = int(row[0]), float(row[1])
        if count >= int(getattr(cfg, "signal_max_open_positions", 3)):
            blockers.append("open_position_limit_reached")
        projected = notional + configured_notional
        if projected > float(getattr(cfg, "signal_max_total_notional", 100)):
            blockers.append("total_notional_limit_exceeded")

    return SignalValidation(
        True,
        not errors,
        not errors and not blockers,
        normalized,
        tuple(dict.fromkeys(errors)),
        tuple(dict.fromkeys(blockers)),
    )
