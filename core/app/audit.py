import logging
from datetime import UTC, datetime

logger = logging.getLogger("roomos.audit")


def command_audit(
    target: str,
    action: str,
    result: str,
    *,
    succeeded: list[str] | None = None,
    skipped: list[str] | None = None,
    failed: list[str] | None = None,
    source: str | None = None,
    routine_id: str | None = None,
) -> None:
    fields = [
        f"timestamp={datetime.now(UTC).isoformat()}",
        f"target={target}",
        f"action={action}",
        f"result={result}",
    ]
    if succeeded is not None:
        fields.append(f"succeeded={','.join(succeeded)}")
    if skipped is not None:
        fields.append(f"skipped={','.join(skipped)}")
    if failed is not None:
        fields.append(f"failed={','.join(failed)}")
    if source is not None:
        fields.append(f"source={source}")
    if routine_id is not None:
        fields.append(f"routine_id={routine_id}")
    logger.info("RoomOS command %s", " ".join(fields))
