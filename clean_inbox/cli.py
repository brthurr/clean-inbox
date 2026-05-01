"""CLI entry point for clean-inbox."""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from clean_inbox import __version__
from clean_inbox.analyzer import AnalysisResult, EmailAnalyzer
from clean_inbox.config import AppConfig, load_config, save_whitelist_entry
from clean_inbox.providers.base import EmailMessage, EmailProvider
from clean_inbox.unsubscriber import UnsubscribeResult, Unsubscriber

app = typer.Typer(
    name="clean-inbox",
    help="Identify and unsubscribe from junk/marketing emails.",
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)


# ---------------------------------------------------------------------------
# Shared options
# ---------------------------------------------------------------------------

ConfigOpt = Annotated[
    Optional[Path],
    typer.Option("--config", "-c", help="Path to config YAML file."),
]
DryRunOpt = Annotated[
    bool,
    typer.Option("--dry-run", "-n", help="Preview actions without executing them."),
]
FolderOpt = Annotated[
    Optional[str],
    typer.Option("--folder", "-f", help="Mailbox folder to scan (default: INBOX)."),
]
MaxOpt = Annotated[
    Optional[int],
    typer.Option("--max", "-m", help="Maximum number of messages to fetch."),
]
ThresholdOpt = Annotated[
    Optional[int],
    typer.Option("--threshold", "-t", help="Junk score threshold 0-100 (default: 30)."),
]


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------


def _build_provider(cfg: AppConfig) -> EmailProvider:
    provider = cfg.provider.lower()
    if provider == "gmail":
        from clean_inbox.providers.gmail import GmailProvider
        return GmailProvider(
            credentials_file=cfg.gmail.credentials_file,
            token_file=cfg.gmail.token_file,
        )
    elif provider == "o365":
        from clean_inbox.providers.o365 import O365Provider
        if not cfg.o365.client_id:
            err_console.print("[red]O365 client_id is required in config.[/red]")
            raise typer.Exit(1)
        return O365Provider(
            client_id=cfg.o365.client_id,
            tenant_id=cfg.o365.tenant_id,
            token_cache_file=cfg.o365.token_cache_file,
        )
    elif provider == "imap":
        from clean_inbox.providers.imap import IMAPProvider
        if not cfg.imap:
            err_console.print("[red]IMAP config section is required when provider = imap.[/red]")
            raise typer.Exit(1)
        return IMAPProvider(
            host=cfg.imap.host,
            username=cfg.imap.username,
            password=cfg.imap.password,
            port=cfg.imap.port,
            use_ssl=cfg.imap.use_ssl,
            trash_folder=cfg.imap.trash_folder,
        )
    else:
        err_console.print(f"[red]Unknown provider: {provider!r}. Choose imap, gmail, or o365.[/red]")
        raise typer.Exit(1)


def _build_unsubscriber(cfg: AppConfig) -> Unsubscriber:
    return Unsubscriber(
        timeout=cfg.unsubscribe_timeout,
        mailto_smtp_host=cfg.mailto.smtp_host or None,
        mailto_smtp_port=cfg.mailto.smtp_port,
        mailto_from=cfg.mailto.from_address or None,
        mailto_password=cfg.mailto.password or None,
    )


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _score_color(score: int) -> str:
    if score >= 70:
        return "red"
    if score >= 40:
        return "yellow"
    return "green"


def _render_senders_table(
    grouped: dict[str, list[AnalysisResult]],
    show_all: bool = False,
) -> Table:
    table = Table(
        title="Identified Junk Senders",
        box=box.ROUNDED,
        show_lines=True,
        expand=True,
    )
    table.add_column("#", style="dim", width=4)
    table.add_column("Sender", min_width=30)
    table.add_column("Msgs", justify="right", width=6)
    table.add_column("Avg Score", justify="right", width=10)
    table.add_column("Unsub?", justify="center", width=8)
    table.add_column("Reasons", min_width=30)

    for idx, (addr, results) in enumerate(grouped.items(), 1):
        avg_score = int(sum(r.score for r in results) / len(results))
        has_unsub = any(r.has_unsubscribe for r in results)
        reasons_set = {reason.value for r in results for reason in r.reasons}
        table.add_row(
            str(idx),
            addr,
            str(len(results)),
            Text(str(avg_score), style=_score_color(avg_score)),
            "[green]Yes[/green]" if has_unsub else "[dim]No[/dim]",
            ", ".join(sorted(reasons_set)),
        )

    return table


def _render_unsub_results(results: list[UnsubscribeResult]) -> None:
    table = Table(title="Unsubscribe Results", box=box.ROUNDED, expand=True)
    table.add_column("Sender", min_width=25)
    table.add_column("Method", min_width=20)
    table.add_column("Status", justify="center", width=10)
    table.add_column("Target / Error", min_width=30)

    for r in results:
        status = "[green]OK (dry run)[/green]" if r.dry_run else (
            "[green]OK[/green]" if r.success else "[red]FAILED[/red]"
        )
        detail = r.target or ""
        if r.error:
            detail = f"[red]{r.error}[/red]"
        table.add_row(r.sender, r.method.value, status, detail)

    console.print(table)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def scan(
    config: ConfigOpt = None,
    dry_run: DryRunOpt = False,
    folder: FolderOpt = None,
    max_messages: MaxOpt = None,
    fetch_all: Annotated[bool, typer.Option("--all", help="Fetch every message in the folder (ignores --max).")] = False,
    threshold: ThresholdOpt = None,
    interactive: Annotated[bool, typer.Option("--interactive", "-i", help="Review each sender before acting.")] = True,
    unsubscribe: Annotated[bool, typer.Option("--unsubscribe/--no-unsubscribe", help="Follow unsubscribe links.")] = True,
    trash: Annotated[bool, typer.Option("--trash/--no-trash", help="Move matched messages to trash.")] = False,
) -> None:
    """Scan inbox, identify junk senders, and optionally unsubscribe."""

    cfg, config_path = load_config(config)
    if folder:
        cfg.folder = folder
    if fetch_all:
        cfg.max_messages = sys.maxsize
    elif max_messages:
        cfg.max_messages = max_messages
    if threshold is not None:
        cfg.junk_threshold = threshold

    console.rule(f"[bold]clean-inbox v{__version__}[/bold]")
    if dry_run:
        console.print(Panel("[yellow bold]DRY RUN MODE — no changes will be made[/yellow bold]", expand=False))

    analyzer = EmailAnalyzer(
        junk_threshold=cfg.junk_threshold,
        whitelist=cfg.whitelist,
        extra_sender_domains=cfg.extra_sender_domains,
        extra_subject_patterns=cfg.extra_subject_patterns,
    )
    unsub = _build_unsubscriber(cfg)

    # ------------------------------------------------------------------
    # 1. Fetch & analyze
    # ------------------------------------------------------------------
    console.print(f"\n[bold]Connecting to [cyan]{cfg.provider}[/cyan] → folder [cyan]{cfg.folder}[/cyan]...[/bold]")

    all_results: list[AnalysisResult] = []
    provider = _build_provider(cfg)

    with provider:
        with console.status(f"Fetching up to {cfg.max_messages} messages..."):
            messages = list(provider.fetch_messages(folder=cfg.folder, max_messages=cfg.max_messages))

        console.print(f"  Fetched [bold]{len(messages)}[/bold] messages. Analyzing...")

        with console.status("Analyzing..."):
            all_results = analyzer.analyze_batch(messages)

    junk_results = [r for r in all_results if r.is_junk]
    console.print(
        f"\n  [green]{len(all_results) - len(junk_results)}[/green] clean  "
        f"[red]{len(junk_results)}[/red] junk  "
        f"(threshold: {cfg.junk_threshold})\n"
    )

    if not junk_results:
        console.print("[green]Your inbox looks clean![/green]")
        raise typer.Exit(0)

    # ------------------------------------------------------------------
    # 2. Group by sender
    # ------------------------------------------------------------------
    grouped: dict[str, list[AnalysisResult]] = defaultdict(list)
    for r in junk_results:
        grouped[r.message.sender_address].append(r)

    # Sort by message count descending
    grouped = dict(sorted(grouped.items(), key=lambda kv: -len(kv[1])))

    console.print(_render_senders_table(grouped))

    # ------------------------------------------------------------------
    # 3. Interactive review
    # ------------------------------------------------------------------
    approved_senders: set[str] = set()
    whitelisted_senders: set[str] = set()

    if interactive:
        console.print(
            "\n[bold]Review each sender:[/bold] "
            "y=act on this sender  n=skip  w=whitelist + delete existing  q=quit\n"
        )
        for addr, results in grouped.items():
            sample = results[0].message
            example_subject = sample.subject[:60] + ("…" if len(sample.subject) > 60 else "")
            has_unsub = any(r.has_unsubscribe for r in results)
            unsub_tag = "[green]✓ unsubscribe link[/green]" if has_unsub else "[dim]no unsub link[/dim]"

            console.print(
                f"  [bold]{addr}[/bold]  {len(results)} msg(s)  {unsub_tag}\n"
                f"  e.g. [dim]\"{example_subject}\"[/dim]"
            )
            choice = Prompt.ask("  Action", choices=["y", "n", "w", "q"], default="y")
            if choice == "q":
                console.print("[dim]Stopping review early.[/dim]")
                break
            elif choice == "y":
                approved_senders.add(addr)
            elif choice == "w":
                whitelisted_senders.add(addr)
                approved_senders.add(addr)  # still trash existing messages
                wl_file = save_whitelist_entry(addr, config_path)
                console.print(f"  [cyan]Whitelisted[/cyan] — saved to {wl_file}")
            console.print()
    else:
        approved_senders = set(grouped.keys())

    if not approved_senders:
        console.print("[dim]No senders approved for action. Done.[/dim]")
        raise typer.Exit(0)

    # ------------------------------------------------------------------
    # 4. Unsubscribe
    # ------------------------------------------------------------------
    unsub_messages: list[EmailMessage] = []
    trash_messages: list[EmailMessage] = []

    for addr in approved_senders:
        for r in grouped[addr]:
            if r.has_unsubscribe:
                unsub_messages.append(r.message)
            if trash:
                trash_messages.append(r.message)

    unsub_ok = unsub_failed = 0
    if unsubscribe and unsub_messages:
        console.print(f"\n[bold]Unsubscribing from {len(unsub_messages)} message(s)...[/bold]")
        # Deduplicate by sender — only need one unsubscribe per sender
        seen_senders: set[str] = set()
        unique_unsub: list[EmailMessage] = []
        for msg in unsub_messages:
            if msg.sender_address not in seen_senders:
                seen_senders.add(msg.sender_address)
                unique_unsub.append(msg)

        unsub_results = unsub.unsubscribe_batch(unique_unsub, dry_run=dry_run)
        _render_unsub_results(unsub_results)
        unsub_ok = sum(1 for r in unsub_results if r.success)
        unsub_failed = sum(1 for r in unsub_results if not r.success)

    # ------------------------------------------------------------------
    # 5. Move to trash
    # ------------------------------------------------------------------
    trashed = 0
    if trash and trash_messages:
        total = len(trash_messages)
        if not dry_run:
            if not Confirm.ask(f"\nMove [red]{total}[/red] messages to trash?"):
                console.print("[dim]Skipping trash.[/dim]")
            else:
                provider = _build_provider(cfg)
                with provider:
                    with console.status(f"Moving {total} messages to trash..."):
                        for msg in trash_messages:
                            provider.move_to_trash(msg)
                trashed = total
                console.print(f"[green]Moved {total} messages to trash.[/green]")
        else:
            trashed = total
            console.print(f"\n[yellow][dry-run] Would move {total} message(s) to trash.[/yellow]")

    # ------------------------------------------------------------------
    # 6. Summary
    # ------------------------------------------------------------------
    skipped_senders = len(grouped) - len(approved_senders)
    flagged_msgs = sum(len(v) for v in grouped.values())
    approved_msgs = sum(len(grouped[a]) for a in approved_senders)

    summary = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    summary.add_column(style="dim")
    summary.add_column(justify="right", style="bold")

    summary.add_row("Emails fetched",        str(len(all_results)))
    summary.add_row("Flagged as junk",       f"[red]{len(junk_results)}[/red]")
    summary.add_row("Clean / whitelisted",   f"[green]{len(all_results) - len(junk_results)}[/green]")
    summary.add_row("Senders reviewed",      str(len(grouped)))
    summary.add_row("Senders approved",      str(len(approved_senders)))
    summary.add_row("Senders whitelisted",   str(len(whitelisted_senders)))
    summary.add_row("Senders skipped",       str(skipped_senders))
    if unsubscribe:
        label = "Unsubscribed (dry run)" if dry_run else "Unsubscribed"
        summary.add_row(label, f"[green]{unsub_ok}[/green]")
        if unsub_failed:
            summary.add_row("Unsubscribe failed", f"[red]{unsub_failed}[/red]")
    if trash:
        label = "Moved to trash (dry run)" if dry_run else "Moved to trash"
        summary.add_row(label, str(trashed))

    console.print()
    console.print(Panel(summary, title="[bold]Summary[/bold]", expand=False))
    console.print("[bold green]Done.[/bold green]")


@app.command()
def senders(
    config: ConfigOpt = None,
    folder: FolderOpt = None,
    max_messages: MaxOpt = None,
    threshold: ThresholdOpt = None,
    min_count: Annotated[int, typer.Option("--min-count", help="Only show senders with at least N messages.")] = 1,
) -> None:
    """List junk senders found in the inbox without taking any action."""

    cfg, _ = load_config(config)
    if folder:
        cfg.folder = folder
    if max_messages:
        cfg.max_messages = max_messages
    if threshold is not None:
        cfg.junk_threshold = threshold

    analyzer = EmailAnalyzer(
        junk_threshold=cfg.junk_threshold,
        whitelist=cfg.whitelist,
        extra_sender_domains=cfg.extra_sender_domains,
        extra_subject_patterns=cfg.extra_subject_patterns,
    )

    console.print(f"[bold]Scanning {cfg.folder} via {cfg.provider}...[/bold]")
    provider = _build_provider(cfg)
    with provider:
        with console.status("Fetching messages..."):
            messages = list(provider.fetch_messages(folder=cfg.folder, max_messages=cfg.max_messages))
        results = analyzer.analyze_batch(messages)

    junk = [r for r in results if r.is_junk]
    grouped: dict[str, list[AnalysisResult]] = defaultdict(list)
    for r in junk:
        grouped[r.message.sender_address].append(r)

    grouped = {k: v for k, v in grouped.items() if len(v) >= min_count}
    grouped = dict(sorted(grouped.items(), key=lambda kv: -len(kv[1])))

    if not grouped:
        console.print("[green]No junk senders found.[/green]")
        return

    console.print(_render_senders_table(grouped))
    console.print(f"\nTotal: [bold]{len(grouped)}[/bold] senders, [bold]{len(junk)}[/bold] messages flagged.")


@app.command()
def unsubscribe_sender(
    address: Annotated[str, typer.Argument(help="Sender email address to unsubscribe from.")],
    config: ConfigOpt = None,
    dry_run: DryRunOpt = False,
    folder: FolderOpt = None,
    max_messages: MaxOpt = None,
) -> None:
    """Unsubscribe from a specific sender address."""

    cfg, _ = load_config(config)
    if folder:
        cfg.folder = folder
    if max_messages:
        cfg.max_messages = max_messages

    address = address.lower().strip()
    provider = _build_provider(cfg)
    unsub = _build_unsubscriber(cfg)

    with provider:
        with console.status(f"Searching for messages from {address}..."):
            messages = [
                m for m in provider.fetch_messages(folder=cfg.folder, max_messages=cfg.max_messages)
                if m.sender_address == address
            ]

    if not messages:
        console.print(f"[yellow]No messages found from {address}.[/yellow]")
        raise typer.Exit(1)

    # Pick the message most likely to have an unsubscribe link
    with_header = [m for m in messages if m.list_unsubscribe]
    target = with_header[0] if with_header else messages[0]

    console.print(f"Found [bold]{len(messages)}[/bold] message(s) from [cyan]{address}[/cyan].")
    if not target.list_unsubscribe:
        console.print("[yellow]No List-Unsubscribe header; will attempt body link extraction.[/yellow]")
        provider2 = _build_provider(cfg)
        with provider2:
            target = provider2.fetch_body(target)

    result = unsub.unsubscribe(target, dry_run=dry_run)
    _render_unsub_results([result])


@app.command()
def version() -> None:
    """Print the version and exit."""
    console.print(f"clean-inbox {__version__}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
