"""Example qdistro hook: clipboard size policy.

Drop this file in /etc/qdistro/hooks/ to automatically deny
large clipboard transfers between different users.  Transfers
within the same user or under the size limit fall through to the
normal admin-prompt path.

Hook functions are named ``on_<action>(event)`` where ``<action>``
matches the broker's action string.  The event dict carries the
details the broker collected from the caller.

Return values:
  - None: fall through to the next hook or the admin prompt.
  - {"action": "allow"}: silently allow.
  - {"action": "deny", "reason": "..."}: silently deny.
  - {"action": "transform", ...}: allow with payload mutation
    (the broker treats this as allow; downstream consumers see
    the transform metadata).
"""


def on_clipboard_send(event):
    """Block large clipboard transfers between different users."""
    source_uid = event.get("source_uid") or event.get("caller_uid")
    target_uid = event.get("target_uid")
    if source_uid is not None and target_uid is not None:
        if int(source_uid) != int(target_uid):
            size = int(event.get("payload_size", 0))
            if size > 1_000_000:
                return {
                    "action": "deny",
                    "reason": "cross-user clipboard limited to 1 MB",
                }
    return None  # fall through to admin prompt
