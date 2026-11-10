from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx


CHUNK_SIZE = 1024 * 1024
FILE_ID = re.compile(r"[0-9a-f]{32}")
BYTE_RANGE = re.compile(r"bytes=([0-9]*)-([0-9]*)")
CONTENT_RANGE = re.compile(r"bytes ([0-9]+)-([0-9]+)/([0-9]+|\*)")

ProgressCallback = Callable[[int, int | None], None]


class TransferError(Exception):
    """An API error or an invalid/incomplete transfer."""


@dataclass(frozen=True)
class UploadResult:
    id: str
    filename: str
    size_bytes: int
    delete_protected: bool
    download_url: str


@dataclass(frozen=True)
class DownloadResult:
    bytes_written: int
    content_range: str | None


def normalize_base_url(value: str) -> str:
    value = value.strip()

    try:
        url = httpx.URL(value)
    except httpx.InvalidURL as exc:
        raise ValueError("Enter a valid HTTP or HTTPS API URL.") from exc

    if (
        url.scheme not in {"http", "https"}
        or not url.host
        or url.userinfo
        or "?" in value
        or "#" in value
    ):
        raise ValueError(
            "Use an HTTP(S) base URL without credentials, query parameters, "
            "or a fragment."
        )

    return str(url).rstrip("/")


def validate_filename(filename: str) -> None:
    if (
        not isinstance(filename, str)
        or not 1 <= len(filename) <= 255
        or any(c in "/\\" or ord(c) < 32 or ord(c) == 127 for c in filename)
    ):
        raise ValueError(
            "Filename must contain 1–255 characters, without directory "
            "separators or control characters."
        )


def password_headers(password: str | None) -> dict[str, str]:
    if password is None:
        return {}

    # HTTP header values do not have a universal Unicode encoding.
    # Restrict passwords to portable, printable ASCII rather than silently
    # changing their encoding or allowing header-control characters.
    if (
        not 1 <= len(password) <= 1024
        or any(not 32 <= ord(c) <= 126 for c in password)
        or password != password.strip()
    ):
        raise ValueError(
            "Deletion passwords must contain 1–1024 printable ASCII "
            "characters, without leading or trailing spaces."
        )

    return {"X-Delete-Password": password}


def normalize_range(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None

    if not value.startswith("bytes="):
        value = "bytes=" + value

    match = BYTE_RANGE.fullmatch(value)
    if not match:
        raise ValueError("Use one byte range: 0-1023, 1024-, or -1024.")

    start, end = match.groups()
    if (
        not (start or end)
        or (start and end and int(end) < int(start))
        or (not start and int(end) == 0)
    ):
        raise ValueError("The byte range is invalid.")

    return value


def origin(url: httpx.URL) -> tuple[str, str, int]:
    port = url.port
    if port is None:
        port = 443 if url.scheme == "https" else 80
    return url.scheme, url.host, port


class QuickTransferClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self._http = httpx.Client(
            base_url=self.base_url + "/",
            headers={
                "Accept-Encoding": "identity",
                "User-Agent": "quick-transfer-cli/0.1.0",
            },
            timeout=httpx.Timeout(
                connect=10.0,
                read=120.0,
                write=120.0,
                pool=10.0,
            ),
            # Do not forward deletion passwords through redirects.
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> QuickTransferClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def resolve_id(self, value: str) -> str:
        """Accept an ID or a file URL belonging to the configured API."""
        value = value.strip()
        if FILE_ID.fullmatch(value):
            return value

        base = httpx.URL(self.base_url + "/")
        try:
            candidate = base.join(value)
        except httpx.InvalidURL:
            raise ValueError("Invalid file ID or download URL.") from None

        prefix = base.path + "files/"
        candidate_id = (
            candidate.path[len(prefix):]
            if candidate.path.startswith(prefix)
            else ""
        )

        if (
            origin(candidate) == origin(base)
            and not candidate.userinfo
            and not candidate.query
            and not candidate.fragment
            and FILE_ID.fullmatch(candidate_id)
        ):
            return candidate_id

        raise ValueError(
            "Enter a 32-character lowercase hexadecimal file ID, or a "
            "download URL from the configured API server."
        )

    @staticmethod
    def _expect(response: httpx.Response, expected: set[int]) -> None:
        status = response.status_code
        if status in expected:
            return

        messages = {
            400: "Invalid request: check the byte range or upload Content-Length.",
            403: "The deletion password is missing or incorrect.",
            404: "File not found.",
            409: "The file is being downloaded. Try deleting it later.",
            413: "The file exceeds the server's configured size limit.",
            415: "The server requires Content-Type: application/octet-stream.",
            416: "The requested byte range is not satisfiable.",
            422: "The server rejected a filename, file ID, range, or password.",
        }

        if 300 <= status < 400:
            message = "Redirect refused. Configure the API's final base URL."
        else:
            message = messages.get(status, response.reason_phrase)

        # Do not print arbitrary error bodies: validation responses can
        # contain reflected inputs, including deletion passwords.
        raise TransferError(f"HTTP {status}: {message}")

    def upload(
        self,
        path: Path,
        filename: str,
        password: str | None = None,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> UploadResult:
        path = Path(path).expanduser()
        validate_filename(filename)

        headers = {
            "Content-Type": "application/octet-stream",
            **password_headers(password),
        }

        if not path.is_file():
            raise ValueError("Select an existing regular file.")

        with path.open("rb") as source:
            size = os.fstat(source.fileno()).st_size
            headers["Content-Length"] = str(size)

            if on_progress:
                on_progress(0, size)

            def chunks():
                remaining = size
                while remaining:
                    chunk = source.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        raise TransferError(
                            "The source file shrank during upload. "
                            "The server's state is uncertain."
                        )

                    yield chunk
                    remaining -= len(chunk)

                    if on_progress:
                        on_progress(size - remaining, size)

            # Raw bytes, not multipart/form-data. Explicit Content-Length
            # prevents chunked transfer encoding for this iterator.
            response = self._http.post(
                "files",
                params={"filename": filename},
                headers=headers,
                content=chunks(),
            )

        self._expect(response, {201})

        try:
            data = response.json()
            result = UploadResult(
                id=data["id"],
                filename=data["filename"],
                size_bytes=data["size_bytes"],
                delete_protected=data["delete_protected"],
                download_url=data["download_url"],
            )
            validate_filename(result.filename)

            if (
                not isinstance(result.id, str)
                or not FILE_ID.fullmatch(result.id)
                or type(result.size_bytes) is not int
                or result.size_bytes != size
                or type(result.delete_protected) is not bool
                or not isinstance(result.download_url, str)
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise TransferError(
                "The server reported upload success but returned an invalid "
                "result. The file may exist; do not retry blindly."
            ) from None

        return result

    def head(
        self,
        file_id: str,
        byte_range: str | None = None,
    ) -> httpx.Response:
        file_id = self.resolve_id(file_id)
        byte_range = normalize_range(byte_range)
        headers = {"Range": byte_range} if byte_range else {}

        response = self._http.head(f"files/{file_id}", headers=headers)
        self._expect(response, {200, 206})
        return response

    def delete(self, file_id: str, password: str | None = None) -> None:
        file_id = self.resolve_id(file_id)
        response = self._http.delete(
            f"files/{file_id}",
            headers=password_headers(password),
        )
        self._expect(response, {204})

    def download(
        self,
        file_id: str,
        destination: Path,
        byte_range: str | None = None,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> DownloadResult:
        """Replace destination only after a successful, complete transfer."""
        file_id = self.resolve_id(file_id)
        byte_range = normalize_range(byte_range)
        headers = {"Range": byte_range} if byte_range else {}

        destination = Path(destination).expanduser()
        if destination.is_dir():
            raise ValueError("The destination must be a filename, not a directory.")
        destination.parent.mkdir(parents=True, exist_ok=True)

        temporary: Path | None = None

        try:
            with self._http.stream(
                "GET", f"files/{file_id}", headers=headers
            ) as response:
                self._expect(response, {200, 206})

                if byte_range and response.status_code != 206:
                    raise TransferError(
                        "The server ignored the requested range. Nothing was saved."
                    )
                if not byte_range and response.status_code != 200:
                    raise TransferError("Unexpected partial response. Nothing was saved.")

                if response.headers.get(
                    "Content-Encoding", "identity"
                ).strip().lower() not in {"", "identity"}:
                    raise TransferError(
                        "The server applied content encoding despite "
                        "Accept-Encoding: identity. Nothing was saved."
                    )

                raw_length = response.headers.get("Content-Length")
                try:
                    total = int(raw_length) if raw_length is not None else None
                    if total is not None and total < 0:
                        raise ValueError
                except ValueError:
                    raise TransferError("Invalid response Content-Length.") from None

                content_range = None
                if response.status_code == 206:
                    content_range = response.headers.get("Content-Range", "")
                    match = CONTENT_RANGE.fullmatch(content_range)
                    if not match:
                        raise TransferError("Missing or invalid Content-Range.")

                    start, end = int(match[1]), int(match[2])
                    whole = match[3]

                    if end < start or (whole != "*" and end >= int(whole)):
                        raise TransferError("Invalid Content-Range bounds.")

                    expected_length = end - start + 1
                    if total is not None and total != expected_length:
                        raise TransferError(
                            "Content-Length disagrees with Content-Range."
                        )
                    total = expected_length

                    # Check the actual range against the requested range.
                    left, right = byte_range[6:].split("-")
                    if left and start != int(left):
                        raise TransferError("The server returned the wrong range offset.")
                    if left and right and end > int(right):
                        raise TransferError("The server returned too large a range.")
                    if not left and expected_length > int(right):
                        raise TransferError("The server returned too large a suffix.")

                    if whole != "*":
                        size = int(whole)
                        expected_start = (
                            int(left) if left else max(0, size - int(right))
                        )
                        expected_end = (
                            min(int(right), size - 1)
                            if left and right
                            else size - 1
                        )
                        if (start, end) != (expected_start, expected_end):
                            raise TransferError(
                                "Content-Range does not match the requested range."
                            )

                written = 0
                if on_progress:
                    on_progress(0, total)

                # Same directory ensures os.replace stays on one filesystem.
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=".quick-transfer-",
                    suffix=".part",
                    dir=destination.parent,
                    delete=False,
                ) as output:
                    temporary = Path(output.name)

                    for chunk in response.iter_bytes(CHUNK_SIZE):
                        written += len(chunk)
                        if total is not None and written > total:
                            raise TransferError(
                                "The response exceeded its advertised length."
                            )

                        output.write(chunk)
                        if on_progress:
                            on_progress(written, total)

                    if total is not None and written != total:
                        raise TransferError(
                            "The download was incomplete. Destination unchanged."
                        )

            # The existing destination remains intact until this point.
            os.replace(temporary, destination)
            return DownloadResult(written, content_range)

        finally:
            # Runs on HTTP errors, disk errors, and Ctrl+C.
            if temporary is not None:
                temporary.unlink(missing_ok=True)