import re


FILENAME_FORMAT_TIMESTAMP = "timestamp"
FILENAME_FORMAT_USERNAME_TIMESTAMP = "username_timestamp"
ALLOWED_FILENAME_FORMATS = {
    FILENAME_FORMAT_TIMESTAMP,
    FILENAME_FORMAT_USERNAME_TIMESTAMP,
}


def normalize_filename_format(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in ALLOWED_FILENAME_FORMATS:
        return normalized
    return FILENAME_FORMAT_TIMESTAMP


def safe_filename_part(value: object, fallback: str = "session") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    cleaned = cleaned.strip("._-")
    return cleaned or fallback


def recording_base_name(
    person: str,
    start_timestamp: str,
    session_id: str,
    filename_format: object,
) -> str:
    if normalize_filename_format(filename_format) == FILENAME_FORMAT_USERNAME_TIMESTAMP:
        return f"{safe_filename_part(person)}_{start_timestamp.replace('_', '-')}"
    return f"{start_timestamp}_{session_id[:6]}"
