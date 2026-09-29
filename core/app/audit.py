import logging
from datetime import UTC, datetime

logger = logging.getLogger("roomos.audit")


def command_audit(
    target: str,
    action: str,
    result: str,
    *,
    succeeded: list[str] | None = None,
    failed: list[str] | None = None,
) -> None:
    fields = [
        f"timestamp={datetime.now(UTC).isoformat()}",
        f"target={target}",
        f"action={action}",
        f"result={result}",
    ]
    if succeeded is not None:
        fields.append(f"succeeded={','.join(succeeded)}")
    if failed is not None:
        fields.append(f"failed={','.join(failed)}")
    logger.info("RoomOS command %s", " ".join(fields))
