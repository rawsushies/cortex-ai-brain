"""Provider adapters. Each declares its real capabilities (see base.Capabilities)."""

from . import claude_code, file_drop

ADAPTERS = {"claude-code": claude_code, "file-drop": file_drop}


def capabilities() -> list[dict]:
    return [m.CAPS.to_dict() for m in ADAPTERS.values()]
