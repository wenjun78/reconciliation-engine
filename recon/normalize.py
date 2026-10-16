"""Transport envelope and payload normalisation.

Every input line becomes exactly one ``Rec``.  Business equality is decided
on the *normalised* record, never on the raw bytes: a ledger v2
``amount_minor_text`` is parsed to an int here, so v1/v2 copies of the same
revision collapse to one fingerprint.

Profiles are reduced to a count at this layer; name and email are never
materialised beyond the parsed JSON object, which is dropped immediately.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional

from .money import parse_int_text

SUPPORTED = {
    ("ledger", 1),
    ("ledger", 2),
    ("merchants", 1),
    ("fx", 1),
    ("profiles", 1),
    ("erasures", 1),
}

KIND_LEDGER = "ledger"
KIND_MERCHANT = "merchant"
KIND_FX = "fx"
KIND_ERASURE = "erasure"
KIND_PROFILE = "profile"
KIND_QUARANTINE = "quarantine"


class InputError(ValueError):
    """Raised for envelope/payload shapes the specification places out of scope."""


@dataclass(frozen=True)
class Rec:
    row_id: str
    stream: str
    schema_version: Any
    kind: str
    # Business key parts, version and fingerprint (None for profile/erasure/quarantine)
    key: tuple
    version: Optional[int]
    fp: Optional[str]
    fields: dict  # normalised business fields (PII-free)


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _req(payload: dict, name: str, typ) -> Any:
    if name not in payload:
        raise InputError(f"payload missing mandatory field {name!r}")
    v = payload[name]
    if typ is int:
        if not _is_int(v):
            raise InputError(f"field {name!r} must be an integer")
    elif not isinstance(v, typ):
        raise InputError(f"field {name!r} must be {typ.__name__}")
    return v


def fingerprint(parts: list) -> str:
    """Stable 128-bit fingerprint of the declared business fields."""
    canon = json.dumps(parts, separators=(",", ":"), ensure_ascii=True)
    return hashlib.blake2b(canon.encode("utf-8"), digest_size=16).hexdigest()


def parse_envelope(obj: Any) -> tuple[int, str, str, Any, Any]:
    if not isinstance(obj, dict):
        raise InputError("line is not a JSON object")
    batch = obj.get("batch")
    if not _is_int(batch) or batch <= 0:
        raise InputError("envelope.batch must be a positive integer")
    row_id = obj.get("row_id")
    if not isinstance(row_id, str) or not row_id:
        raise InputError("envelope.row_id must be a non-empty string")
    stream = obj.get("stream")
    if not isinstance(stream, str):
        raise InputError("envelope.stream must be a string")
    if "payload" not in obj:
        raise InputError("envelope.payload missing")
    return batch, row_id, stream, obj.get("schema_version"), obj["payload"]


def normalise(obj: Any) -> tuple[int, Rec]:
    """Return (batch, Rec) for one parsed JSON line."""
    batch, row_id, stream, schema_version, payload = parse_envelope(obj)
    sv = schema_version if _is_int(schema_version) else None

    if (stream, sv) not in SUPPORTED:
        return batch, Rec(row_id, stream, schema_version, KIND_QUARANTINE, (row_id,), None, None, {})

    if not isinstance(payload, dict):
        raise InputError(f"{stream} v{sv} payload must be an object")

    if stream == "ledger":
        entry_id = _req(payload, "entry_id", str)
        version = _req(payload, "version", int)
        op = _req(payload, "op", str)
        if op not in ("POST", "VOID"):
            raise InputError("ledger.op must be POST or VOID")
        merchant_id = _req(payload, "merchant_id", str)
        customer_token = _req(payload, "customer_token", str)
        effective_at = _req(payload, "effective_at", str)
        currency = _req(payload, "currency", str)
        if sv == 1:
            amount = _req(payload, "amount_minor", int)
        else:
            amount = parse_int_text(_req(payload, "amount_minor_text", str))
        if version <= 0:
            raise InputError("ledger.version must be positive")
        fields = {
            "op": op,
            "merchant_id": merchant_id,
            "customer_token": customer_token,
            "effective_at": effective_at,
            "currency": currency,
            "amount_minor": amount,
        }
        fp = fingerprint([op, merchant_id, customer_token, effective_at, currency, str(amount)])
        return batch, Rec(row_id, stream, sv, KIND_LEDGER, (entry_id,), version, fp, fields)

    if stream == "merchants":
        merchant_id = _req(payload, "merchant_id", str)
        valid_from = _req(payload, "valid_from", str)
        valid_to = _req(payload, "valid_to", str)
        version = _req(payload, "version", int)
        region = _req(payload, "region", str)
        tier = _req(payload, "tier", str)
        if version <= 0:
            raise InputError("merchants.version must be positive")
        fields = {"valid_to": valid_to, "region": region, "tier": tier}
        fp = fingerprint([valid_to, region, tier])
        return batch, Rec(row_id, stream, sv, KIND_MERCHANT, (merchant_id, valid_from), version, fp, fields)

    if stream == "fx":
        currency = _req(payload, "currency", str)
        date = _req(payload, "date", str)
        version = _req(payload, "version", int)
        num = _req(payload, "usd_micro_num", int)
        den = _req(payload, "usd_micro_den", int)
        if version <= 0 or num <= 0 or den <= 0:
            raise InputError("fx.version/num/den must be positive")
        fields = {"num": num, "den": den}
        fp = fingerprint([str(num), str(den)])
        return batch, Rec(row_id, stream, sv, KIND_FX, (currency, date), version, fp, fields)

    if stream == "profiles":
        _req(payload, "customer_token", str)
        # name/email intentionally dropped here; nothing downstream sees them.
        return batch, Rec(row_id, stream, sv, KIND_PROFILE, (), None, None, {})

    # erasures
    token = _req(payload, "customer_token", str)
    return batch, Rec(row_id, stream, sv, KIND_ERASURE, (token,), None, None, {})


def rec_to_json(rec: Rec) -> str:
    """Serialise the PII-free normalised record for durable staging."""
    doc = {
        "row_id": rec.row_id,
        "stream": rec.stream,
        "schema_version": rec.schema_version,
        "kind": rec.kind,
        "key": list(rec.key),
        "version": rec.version,
        "fp": rec.fp,
        "fields": {k: (str(v) if isinstance(v, int) and not isinstance(v, bool) else v) for k, v in rec.fields.items()},
    }
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=True)


def rec_from_json(text: str) -> Rec:
    d = json.loads(text)
    fields = dict(d["fields"])
    for k in ("amount_minor", "num", "den"):
        if k in fields:
            fields[k] = int(fields[k])
    return Rec(d["row_id"], d["stream"], d["schema_version"], d["kind"], tuple(d["key"]), d["version"], d["fp"], fields)
