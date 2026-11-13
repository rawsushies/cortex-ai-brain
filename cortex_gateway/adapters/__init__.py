"""Provider adapters. Each declares its real capabilities (see base.Capabilities)."""

from . import claude_code, exports, file_drop, local_apps

ADAPTERS = {"claude-code": claude_code, "file-drop": file_drop}
CAPABILITIES = [claude_code.CAPS, local_apps.CODEX, local_apps.GEMINI_CLI, local_apps.CURSOR,
                exports.CHATGPT, exports.CLAUDE_AI, exports.GEMINI_TAKEOUT, file_drop.CAPS]


def capabilities() -> list[dict]:
    return [c.to_dict() for c in CAPABILITIES]
