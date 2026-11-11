"""Isolated PDF text extraction: ``python -I -m rfq_copilot.pdf_worker`` (stdin -> stdout).

Run by ``attachments.extract_pdf_isolated`` in a child process with an empty
environment, a CPU-time limit and (where the OS enforces it) an address-space limit. The
parent kills it at a wall-clock deadline, so a hostile PDF cannot keep a server thread
busy forever. Output is one JSON object; nothing else is written.
"""

import json
import resource
import sys

CPU_SECONDS = 10
ADDRESS_SPACE_BYTES = 1024 * 1024 * 1024


def _limit() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    try:  # enforced on Linux; macOS refuses or ignores it
        resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE_BYTES, ADDRESS_SPACE_BYTES))
    except (ValueError, OSError):
        pass


def main() -> int:
    _limit()
    from rfq_copilot.attachments import AttachmentRejected, extract_pdf

    data = sys.stdin.buffer.read()
    try:
        result = extract_pdf(data)
    except AttachmentRejected as rejected:
        out = {"rejected": rejected.reason}
    else:
        out = {
            "status": result.status,
            "page_count": result.page_count,
            "pages": [[page.page_number, page.text] for page in result.pages],
            "truncated": result.truncated,
        }
    sys.stdout.write(json.dumps(out))  # ASCII-escaped
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
