"""Minimal environment for the test MCP subprocess; never forward administrative secrets."""

OS_KEYS = ("PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL")
DATABASE_KEYS = (
    "TEST_APP_DATABASE_URL",
    "TEST_DATABASE_NAME",
    "DEVELOPMENT_DATABASE_NAME",
    "DATABASE_HOST",
    "DATABASE_PORT",
)


def mcp_process_environment(file_values, process_values):
    selected = {k: process_values[k] for k in OS_KEYS if k in process_values}
    for key in DATABASE_KEYS:
        value = process_values[key] if key in process_values else file_values.get(key)
        if value is not None:
            selected[key] = value
    return selected
