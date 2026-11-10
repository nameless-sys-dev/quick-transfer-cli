from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
from platformdirs import user_config_path
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from .api import (
    QuickTransferClient,
    TransferError,
    normalize_base_url,
    normalize_range,
    validate_filename,
)


console = Console()
CONFIG = user_config_path("quick-transfer", appauthor=False) / "config.json"


def ask(
    label: str,
    default: str | None = None,
    *,
    password: bool = False,
) -> str:
    # Text prevents filenames, URLs, etc. from being treated as Rich markup.
    prompt = Text(label)
    if default is not None:
        prompt.append(f" [{default}]", style="dim")

    answer = Prompt.ask(prompt, password=password, console=console)
    return answer if answer else (default or "")


def path_prompt(label: str, default: str | None = None) -> Path:
    value = ask(label, default).strip()

    # Accept paths pasted with matching surrounding quotes.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]

    if not value:
        raise ValueError("A file path is required.")

    return Path(value).expanduser()


def default_server() -> str:
    if value := os.getenv("QUICK_TRANSFER_URL"):
        return value

    try:
        value = json.loads(CONFIG.read_text(encoding="utf-8"))["base_url"]
        if isinstance(value, str):
            return value
    except (OSError, ValueError, KeyError, TypeError):
        pass

    return "http://localhost:8000"


def choose_server(default: str) -> str:
    while True:
        try:
            value = normalize_base_url(
                ask("API base URL, without the /files endpoint", default)
            )
            break
        except ValueError as exc:
            console.print(Text(str(exc), style="red"))

    try:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(
            json.dumps({"base_url": value}, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        console.print(
            "[yellow]Could not save settings; using this URL for this session.[/yellow]"
        )

    if value.startswith("http://"):
        console.print(
            "[yellow]HTTP is unencrypted. Prefer HTTPS for remote servers, "
            "especially when using deletion passwords.[/yellow]"
        )

    return value


def make_progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    )


def ask_range() -> str | None:
    return normalize_range(
        ask("Byte range (blank = all; examples: 0-1023, 1024-, -1024)")
    )


def upload_file(api: QuickTransferClient) -> None:
    path = path_prompt("Local file path")
    if not path.is_file():
        raise ValueError("Select an existing regular file.")

    filename = ask("Download filename", path.name)
    validate_filename(filename)

    console.print(
        "[dim]A password protects deletion only. Anyone with the link "
        "can download the file.[/dim]"
    )
    password = ask("Deletion password (blank = none)", password=True) or None
    if password and password != ask("Repeat deletion password", password=True):
        raise ValueError("Passwords do not match.")

    with make_progress() as progress:
        task = progress.add_task("Uploading", total=None)
        result = api.upload(
            path,
            filename,
            password,
            on_progress=lambda done, total: progress.update(
                task, completed=done, total=total
            ),
        )

    table = Table(title="Upload complete", show_header=False)
    table.add_column(style="cyan", no_wrap=True)
    table.add_column(overflow="fold")

    for label, value in [
        ("File ID", result.id),
        ("Filename", result.filename),
        ("Size (bytes)", f"{result.size_bytes:,}"),
        ("Deletion protected", "Yes" if result.delete_protected else "No"),
        ("Download URL", result.download_url),
    ]:
        table.add_row(label, Text(value))

    console.print(table)
    console.print(
        "[dim]Save the ID or link. The API does not provide a file-listing endpoint.[/dim]"
    )


def download_file(api: QuickTransferClient) -> None:
    file_id = api.resolve_id(ask("File ID or download URL"))
    byte_range = ask_range()

    suffix = ".range" if byte_range else ".bin"
    destination = path_prompt("Save as", file_id + suffix)

    if destination.is_dir():
        raise ValueError("Specify a destination filename, not a directory.")

    if destination.exists() or destination.is_symlink():
        console.print(Text(f"Destination: {destination}"))
        if not Confirm.ask(
            "Replace this file after a successful download?",
            default=False,
            console=console,
        ):
            return

    with make_progress() as progress:
        task = progress.add_task("Downloading", total=None)
        result = api.download(
            file_id,
            destination,
            byte_range,
            on_progress=lambda done, total: progress.update(
                task, completed=done, total=total
            ),
        )

    console.print(
        Text(
            f"Saved {result.bytes_written:,} bytes to {destination}",
            style="green",
        )
    )

    if result.content_range:
        console.print(
            Text(
                f"Partial file: {result.content_range}. "
                "Range downloads are separate files, not automatic resumes.",
                style="yellow",
            )
        )


def file_information(api: QuickTransferClient) -> None:
    file_id = api.resolve_id(ask("File ID or download URL"))
    response = api.head(file_id, ask_range())

    table = Table(title="File information")
    table.add_column("Header / field", style="cyan", no_wrap=True)
    table.add_column("Value", overflow="fold")
    table.add_row("File ID", Text(file_id))
    table.add_row("HTTP status", Text(str(response.status_code)))

    for name, value in response.headers.items():
        table.add_row(Text(name), Text(value))

    console.print(table)


def delete_file(api: QuickTransferClient) -> None:
    file_id = api.resolve_id(ask("File ID or download URL"))
    console.print(Text(f"File to delete: {file_id}"))

    if not Confirm.ask(
        "Permanently delete this file?",
        default=False,
        console=console,
    ):
        return

    password = ask(
        "Deletion password (blank if unprotected)",
        password=True,
    ) or None

    api.delete(file_id, password)
    console.print("[green]File deleted.[/green]")


def show_menu(api: QuickTransferClient) -> str:
    console.print(
        Panel(
            Text(
                f"Server: {api.base_url}\n"
                "Downloads are public. Passwords protect deletion only."
            ),
            title="Quick Transfer",
            border_style="cyan",
        )
    )

    menu = Table.grid(padding=(0, 2))
    for key, label in [
        ("1", "Upload a file"),
        ("2", "Download a file"),
        ("3", "Inspect file information (HEAD)"),
        ("4", "Delete a file"),
        ("5", "Change API server"),
        ("0", "Quit"),
    ]:
        menu.add_row(f"[cyan]{key}[/cyan]", label)

    console.print(menu)
    return Prompt.ask(
        "Choose",
        choices=["1", "2", "3", "4", "5", "0"],
        show_choices=False,
        console=console,
    )


def report_error(message: str, *, uncertain: bool = False) -> None:
    console.print(Panel(Text(message), title="Operation error", border_style="red"))

    if uncertain:
        console.print(
            "[yellow]If an upload or delete reached the server, it may have "
            "completed despite this error. Check before retrying. "
            "Requests are not automatically retried.[/yellow]"
        )


def main() -> None:
    api: QuickTransferClient | None = None
    actions = {
        "1": upload_file,
        "2": download_file,
        "3": file_information,
        "4": delete_file,
    }

    console.print("[bold cyan]Quick Transfer — interactive file transfers[/bold cyan]")
    console.print("[dim]Ctrl+C cancels an action; Ctrl+C at the menu exits.[/dim]")

    try:
        api = QuickTransferClient(choose_server(default_server()))

        while True:
            choice = show_menu(api)
            if choice == "0":
                break

            try:
                if choice == "5":
                    replacement = QuickTransferClient(
                        choose_server(api.base_url)
                    )
                    api.close()
                    api = replacement
                else:
                    actions[choice](api)

            except KeyboardInterrupt:
                report_error(
                    "Operation cancelled.",
                    uncertain=choice in {"1", "4"},
                )
            except (TransferError, httpx.HTTPError, OSError, ValueError) as exc:
                report_error(
                    str(exc),
                    uncertain=choice in {"1", "4"},
                )

    except (KeyboardInterrupt, EOFError):
        pass
    except (TransferError, httpx.HTTPError, OSError, ValueError) as exc:
        report_error(str(exc))
    finally:
        if api is not None:
            api.close()
        console.print("\n[dim]Goodbye.[/dim]")