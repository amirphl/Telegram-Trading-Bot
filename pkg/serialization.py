import base64
import json
from datetime import datetime
from typing import Any


def _to_jsonable(obj: Any):
    if obj is None:
        return None
    if isinstance(obj, datetime):
        return obj.replace(tzinfo=None).isoformat()
    if isinstance(obj, bytes):
        return {"__bytes_b64__": base64.b64encode(obj).decode("ascii")}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return _to_jsonable(to_dict())
    return obj


def dumps_json(obj: Any) -> str:
    return json.dumps(_to_jsonable(obj), ensure_ascii=False) 