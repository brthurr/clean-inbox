"""CLI entry point for clean-inbox."""

from __future__ import annotations

import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
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
from clean_inbox.config import AppConfig, load_config, load_processed, save_processed_entry, save_whitelist_entry
from clean_inbox.providers.base import EmailMessage, EmailProvider
from clean_inbox.unsubscriber import UnsubscribeResult, Unsubscriber

_APP_HELP = """\
Scan your inbox for junk and marketing emails, review identified senders,
automatically unsubscribe, and optionally move messages to trash or delete them.

[bold]Supported providers:[/bold] Gmail · Microsoft 365 / Outlook · any IMAP server

[bold]Typical workflow:[/bold]

  1. Run a dry-run first to see what would be flagged:

       [cyan]clean-inbox scan --dry-run[/cyan]

  2. Review the sender table, then run for real:

       [cyan]clean-inbox scan --trash[/cyan]

  3. To clean your entire mailbox at once:

       [cyan]clean-inbox scan --all --trash[/cyan]

[bold]Junk scoring (0–100):[/bold]

  +60  List-Unsubscribe header present (RFC 2369)
  +30  Precedence: bulk / list / junk
  +40  Sender domain is a known marketing ESP
  +20  X-Mailer matches a marketing tool
  +15  Subject matches a marketing keyword pattern

  Messages scoring ≥ threshold (default 30) are flagged. Whitelisted
  senders are always skipped regardless of score.

[bold]Config file locations (checked in order):[/bold]

  ./clean-inbox.yaml  ·  ./clean-inbox.yml
  ~/.config/clean-inbox/config.yaml

Copy [cyan]clean-inbox.example.yaml[/cyan] to get started.
"""

app = typer.Typer(
    name="clean-inbox",
    help=_APP_HELP,
    add_completion=False,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
    epilog="Run [cyan]clean-inbox COMMAND --help[/cyan] for per-command details.",
)
console = Console()
err_console = Console(stderr=True)


# ---------------------------------------------------------------------------
# Shared options
# ---------------------------------------------------------------------------

ConfigOpt = Annotated[
    Optional[Path],
    typer.Option("--config", "-c", help="Path to a config YAML file. Overrides the default search path."),
]
DryRunOpt = Annotated[
    bool,
    typer.Option("--dry-run", "-n", help="Preview all actions without making any changes. Safe to run at any time."),
]
FolderOpt = Annotated[
    Optional[str],
    typer.Option("--folder", "-f", help="Mailbox folder to scan. Overrides the value in config. [dim]Default: INBOX[/dim]"),
]
MaxOpt = Annotated[
    Optional[int],
    typer.Option("--max", "-m", help="Maximum number of messages to fetch (most recent first). Overrides config. [dim]Default: 500[/dim]"),
]
ThresholdOpt = Annotated[
    Optional[int],
    typer.Option("--threshold", "-t", help="Junk confidence score (0–100). Messages at or above this are flagged. Lower = broader catch. [dim]Default: 30[/dim]"),
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


def _build_unsubscriber(cfg: AppConfig, provider: EmailProvider | None = None) -> Unsubscriber:
    from clean_inbox.providers.gmail import GmailProvider
    send_fn = provider.send_message if isinstance(provider, GmailProvider) else None
    return Unsubscriber(
        timeout=cfg.unsubscribe_timeout,
        mailto_smtp_host=cfg.mailto.smtp_host or None,
        mailto_smtp_port=cfg.mailto.smtp_port,
        mailto_from=cfg.mailto.from_address or None,
        mailto_password=cfg.mailto.password or None,
        send_fn=send_fn,
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
# Logging
# ---------------------------------------------------------------------------

_log = logging.getLogger("clean_inbox")

DEFAULT_LOG_FILE = Path("clean-inbox.log")


def _setup_logging(log_file: Path, dry_run: bool) -> None:
    """Configure file logging. One run = one block of entries separated by a blank line."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    _log.addHandler(handler)
    _log.setLevel(logging.DEBUG)
    mode = " [DRY RUN]" if dry_run else ""
    _log.info("=" * 60)
    _log.info("clean-inbox run started%s", mode)


def _log_unsub_results(results: list) -> None:
    for r in results:
        if r.dry_run:
            _log.info("UNSUB DRY-RUN  %-40s  method=%s  target=%s", r.sender, r.method.value, r.target)
        elif r.success:
            _log.info("UNSUB OK       %-40s  method=%s  target=%s", r.sender, r.method.value, r.target)
        else:
            _log.error("UNSUB FAILED   %-40s  method=%s  error=%s  target=%s",
                       r.sender, r.method.value, r.error, r.target)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command(
    epilog=(
        "[bold]Examples:[/bold]\n\n"
        "  Preview what would be flagged (no changes):\n"
        "    [cyan]clean-inbox scan --dry-run[/cyan]\n\n"
        "  Interactive review, then unsubscribe and trash:\n"
        "    [cyan]clean-inbox scan --trash[/cyan]\n\n"
        "  Scan entire inbox non-interactively and permanently delete:\n"
        "    [cyan]clean-inbox scan --all --no-interactive --delete[/cyan]\n\n"
        "  Use a specific config and raise the sensitivity threshold:\n"
        "    [cyan]clean-inbox scan --config ~/my-config.yaml --threshold 20[/cyan]"
    ),
)
def scan(
    config: ConfigOpt = None,
    dry_run: DryRunOpt = False,
    folder: FolderOpt = None,
    max_messages: MaxOpt = None,
    fetch_all: Annotated[bool, typer.Option("--all", help="Fetch every message in the folder, ignoring --max. Use for a full inbox clean-up.")] = False,
    threshold: ThresholdOpt = None,
    interactive: Annotated[bool, typer.Option("--interactive/--no-interactive", "-i", help="Prompt to approve or skip each identified sender before acting. Disable for scripted/unattended runs. [dim]Default: on[/dim]")] = True,
    unsubscribe: Annotated[bool, typer.Option("--unsubscribe/--no-unsubscribe", help="Follow unsubscribe links for approved senders. Sends one request per sender (not per message). [dim]Default: on[/dim]")] = True,
    trash: Annotated[bool, typer.Option("--trash/--no-trash", help="Move all messages from approved senders to the Trash folder. Recoverable; providers typically purge trash after 30 days. [dim]Default: off[/dim]")] = False,
    delete: Annotated[bool, typer.Option("--delete", help="Permanently delete all messages from approved senders. Cannot be undone. Prompts for confirmation before acting. [dim]Default: off[/dim]")] = False,
    reprocess: Annotated[bool, typer.Option("--reprocess", help="Show all senders again, ignoring the already-processed list.")] = False,
    log_file: Annotated[Path, typer.Option("--log-file", help="Path to the log file. All actions and failures are appended here. [dim]Default: clean-inbox.log[/dim]")] = DEFAULT_LOG_FILE,
) -> None:
    """\
    Fetch messages, score each one for junk signals, group by sender, and act.

    [bold]Steps:[/bold]

      1. [bold]Fetch[/bold]  — pulls up to --max messages from the folder (most recent first).
         Use --all to fetch everything with no limit.

      2. [bold]Analyze[/bold] — scores each message 0–100 using header signals, sender domain
         databases, and subject-line patterns. Messages at or above --threshold
         are flagged.

      3. [bold]Review[/bold] — displays a table of identified senders (one row per sender,
         regardless of how many messages they sent). In interactive mode you
         choose what to do with each:

           [green]y[/green]  act on this sender (unsubscribe + trash/delete if enabled)
           [cyan]c[/cyan]  clean only: trash/delete messages but skip unsubscribe
           [yellow]n[/yellow]  skip this sender for now
           [cyan]w[/cyan]  whitelist: never flag again, but still trash existing messages
           [red]q[/red]  stop reviewing (already-approved senders are still acted on)

      4. [bold]Unsubscribe[/bold] — for each approved sender that has a List-Unsubscribe
         header, sends one unsubscribe request using the best available method:
         RFC 8058 one-click POST → HTTP GET → mailto → body link.

      5. [bold]Trash / Delete[/bold] — moves or permanently removes all messages in the
         current batch from approved senders.

      6. [bold]Summary[/bold] — prints a table showing counts for every action taken.

    [bold]Note:[/bold] --trash and --delete are opt-in. A bare [cyan]clean-inbox scan[/cyan] will
    analyze and unsubscribe but will not touch your messages.
    """

    cfg, config_path = load_config(config)
    if folder:
        cfg.folder = folder
    if fetch_all:
        cfg.max_messages = sys.maxsize
    elif max_messages:
        cfg.max_messages = max_messages
    if threshold is not None:
        cfg.junk_threshold = threshold

    _setup_logging(log_file, dry_run)

    console.rule(f"[bold]clean-inbox v{__version__}[/bold]")
    if dry_run:
        console.print(Panel("[yellow bold]DRY RUN MODE — no changes will be made[/yellow bold]", expand=False))
    console.print(f"[dim]Logging to {log_file}[/dim]")

    analyzer = EmailAnalyzer(
        junk_threshold=cfg.junk_threshold,
        whitelist=cfg.whitelist,
        extra_sender_domains=cfg.extra_sender_domains,
        extra_subject_patterns=cfg.extra_subject_patterns,
    )
    # ------------------------------------------------------------------
    # 1. Fetch & analyze
    # ------------------------------------------------------------------
    console.print(f"\n[bold]Connecting to [cyan]{cfg.provider}[/cyan] → folder [cyan]{cfg.folder}[/cyan]...[/bold]")

    all_results: list[AnalysisResult] = []
    provider = _build_provider(cfg)
    unsub = _build_unsubscriber(cfg, provider)

    with provider:
        fetch_label = "all" if fetch_all else f"up to {cfg.max_messages}"
        with console.status(f"Fetching {fetch_label} messages..."):
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
    processed_senders = set() if reprocess else load_processed(config_path, command="scan")
    if reprocess:
        console.print("[dim]--reprocess: ignoring previously processed senders[/dim]")
    approved_senders: set[str] = set()
    clean_only_senders: set[str] = set()  # trash but don't unsubscribe
    auto_approved_senders: set[str] = set()
    whitelisted_senders: set[str] = set()

    # Auto-approve senders that were approved in a previous run
    for addr in grouped:
        if addr in processed_senders:
            approved_senders.add(addr)
            auto_approved_senders.add(addr)

    new_senders = {addr: results for addr, results in grouped.items() if addr not in processed_senders}

    if auto_approved_senders:
        console.print(
            f"\n[dim]Auto-approving [bold]{len(auto_approved_senders)}[/bold] previously processed "
            f"sender(s) — skipping review for these.[/dim]"
        )

    if interactive and new_senders:
        console.print(
            "\n[bold]Review new senders:[/bold] "
            "y=unsubscribe+clean  c=clean only (no unsub)  n=skip  w=whitelist+clean  q=quit\n"
        )
        for addr, results in new_senders.items():
            sample = results[0].message
            example_subject = sample.subject[:60] + ("…" if len(sample.subject) > 60 else "")
            has_unsub = any(r.has_unsubscribe for r in results)
            unsub_tag = "[green]✓ unsubscribe link[/green]" if has_unsub else "[dim]no unsub link[/dim]"

            console.print(
                f"  [bold]{addr}[/bold]  {len(results)} msg(s)  {unsub_tag}\n"
                f"  e.g. [dim]\"{example_subject}\"[/dim]"
            )
            choice = Prompt.ask("  Action", choices=["y", "c", "n", "w", "q"], default="y")
            if choice == "q":
                console.print("[dim]Stopping review early.[/dim]")
                break
            elif choice == "y":
                approved_senders.add(addr)
                save_processed_entry(addr, config_path, command="scan")
            elif choice == "c":
                approved_senders.add(addr)
                clean_only_senders.add(addr)
                save_processed_entry(addr, config_path, command="scan")
            elif choice == "w":
                whitelisted_senders.add(addr)
                approved_senders.add(addr)
                wl_file = save_whitelist_entry(addr, config_path)
                console.print(f"  [cyan]Whitelisted[/cyan] — saved to {wl_file}")
            console.print()
    elif not interactive:
        for addr in new_senders:
            approved_senders.add(addr)
            save_processed_entry(addr, config_path, command="scan")

    if not approved_senders:
        console.print("[dim]No senders approved for action. Done.[/dim]")
        raise typer.Exit(0)

    # ------------------------------------------------------------------
    # 4. Unsubscribe
    # ------------------------------------------------------------------
    # Only unsubscribe from senders newly approved this run — auto-approved
    # senders were already unsubscribed in a previous run.
    newly_approved = approved_senders - auto_approved_senders

    unsub_messages: list[EmailMessage] = []
    trash_messages: list[EmailMessage] = []
    delete_messages: list[EmailMessage] = []

    for addr in approved_senders:
        for r in grouped[addr]:
            if r.has_unsubscribe and addr in newly_approved and addr not in clean_only_senders:
                unsub_messages.append(r.message)
            if trash:
                trash_messages.append(r.message)
            if delete:
                delete_messages.append(r.message)

    unsub_ok = unsub_failed = 0
    if unsubscribe and unsub_messages:
        # Deduplicate by sender — only one unsubscribe request per sender
        seen_senders: set[str] = set()
        unique_unsub: list[EmailMessage] = []
        for msg in unsub_messages:
            if msg.sender_address not in seen_senders:
                seen_senders.add(msg.sender_address)
                unique_unsub.append(msg)

        console.print(
            f"\n[bold]Sending [cyan]{len(unique_unsub)}[/cyan] unsubscribe request(s) "
            f"[dim]({len(unsub_messages)} messages matched, one request per sender)[/dim]...[/bold]"
        )
        unsub_results = unsub.unsubscribe_batch(unique_unsub, dry_run=dry_run)
        _render_unsub_results(unsub_results)
        _log_unsub_results(unsub_results)
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
                for msg in trash_messages:
                    _log.info("TRASHED        %-40s  subject=%r", msg.sender_address, msg.subject[:80])
        else:
            trashed = total
            console.print(f"\n[yellow][dry-run] Would move {total} message(s) to trash.[/yellow]")
            for msg in trash_messages:
                _log.info("TRASH DRY-RUN  %-40s  subject=%r", msg.sender_address, msg.subject[:80])

    # ------------------------------------------------------------------
    # 6. Permanently delete
    # ------------------------------------------------------------------
    deleted = 0
    if delete and delete_messages:
        total = len(delete_messages)
        if not dry_run:
            if not Confirm.ask(
                f"\n[bold red]Permanently delete {total} message(s)? This cannot be undone.[/bold red]"
            ):
                console.print("[dim]Skipping permanent delete.[/dim]")
            else:
                provider = _build_provider(cfg)
                with provider:
                    with console.status(f"Permanently deleting {total} messages..."):
                        for msg in delete_messages:
                            provider.delete_permanently(msg)
                deleted = total
                console.print(f"[green]Permanently deleted {total} messages.[/green]")
                for msg in delete_messages:
                    _log.info("DELETED        %-40s  subject=%r", msg.sender_address, msg.subject[:80])
        else:
            deleted = total
            console.print(f"\n[yellow][dry-run] Would permanently delete {total} message(s).[/yellow]")
            for msg in delete_messages:
                _log.info("DELETE DRY-RUN %-40s  subject=%r", msg.sender_address, msg.subject[:80])

    # ------------------------------------------------------------------
    # 7. Summary
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
    summary.add_row("Senders found",          str(len(grouped)))
    summary.add_row("Auto-approved (seen before)", str(len(auto_approved_senders)))
    summary.add_row("New senders reviewed",  str(len(new_senders)))
    summary.add_row("Senders approved",      str(len(approved_senders)))
    summary.add_row("Senders clean-only",    str(len(clean_only_senders)))
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
    if delete:
        label = "Permanently deleted (dry run)" if dry_run else "Permanently deleted"
        summary.add_row(label, f"[red]{deleted}[/red]")

    console.print()
    console.print(Panel(summary, title="[bold]Summary[/bold]", expand=False))
    console.print("[bold green]Done.[/bold green]")

    _log.info(
        "Run complete — fetched=%d flagged=%d unsubOK=%d unsubFailed=%d trashed=%d deleted=%d",
        len(all_results), len(junk_results), unsub_ok, unsub_failed, trashed, deleted,
    )
    _log.info("")  # blank line between runs


@app.command(
    epilog=(
        "[bold]Examples:[/bold]\n\n"
        "  List all flagged senders in INBOX:\n"
        "    [cyan]clean-inbox senders[/cyan]\n\n"
        "  Only show senders with 5 or more messages:\n"
        "    [cyan]clean-inbox senders --min-count 5[/cyan]\n\n"
        "  Scan a different folder with a stricter threshold:\n"
        "    [cyan]clean-inbox senders --folder Promotions --threshold 50[/cyan]"
    ),
)
def senders(
    config: ConfigOpt = None,
    folder: FolderOpt = None,
    max_messages: MaxOpt = None,
    threshold: ThresholdOpt = None,
    min_count: Annotated[int, typer.Option("--min-count", help="Only show senders that have at least N messages in the batch. Useful for filtering noise. [dim]Default: 1[/dim]")] = 1,
) -> None:
    """\
    Read-only scan: list every identified junk sender without taking any action.

    Connects to your mailbox, fetches up to --max messages, scores each one,
    then prints a table grouped by sender showing message count, average junk
    score, whether an unsubscribe link was found, and the reasons it was flagged.

    Nothing is modified. Use this command to explore what [cyan]scan[/cyan] would act on
    before committing to any changes.
    """

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


@app.command(
    epilog=(
        "[bold]Examples:[/bold]\n\n"
        "  Preview unsubscribe without sending anything:\n"
        "    [cyan]clean-inbox unsubscribe-sender newsletters@store.com --dry-run[/cyan]\n\n"
        "  Unsubscribe for real:\n"
        "    [cyan]clean-inbox unsubscribe-sender newsletters@store.com[/cyan]"
    ),
)
def unsubscribe_sender(
    address: Annotated[str, typer.Argument(help="The sender email address to unsubscribe from (e.g. newsletters@example.com).")],
    config: ConfigOpt = None,
    dry_run: DryRunOpt = False,
    folder: FolderOpt = None,
    max_messages: MaxOpt = None,
) -> None:
    """\
    Unsubscribe from a single known sender without scanning the full inbox.

    Searches the folder for messages from ADDRESS, picks the one most likely
    to carry an unsubscribe mechanism, and attempts to unsubscribe using the
    best available method:

      1. RFC 8058 one-click POST (List-Unsubscribe-Post header)
      2. HTTP GET  (List-Unsubscribe header with an https: link)
      3. mailto    (List-Unsubscribe header with a mailto: link)
      4. Body link (scans the email HTML/text for an unsubscribe URL)

    Use --dry-run to see which method and URL would be used without sending
    any request.
    """

    cfg, _ = load_config(config)
    if folder:
        cfg.folder = folder
    if max_messages:
        cfg.max_messages = max_messages

    address = address.lower().strip()
    provider = _build_provider(cfg)
    unsub = _build_unsubscriber(cfg, provider)

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
    """Print the version number and exit."""
    console.print(f"clean-inbox {__version__}")


@app.command(
    epilog=(
        "[bold]Examples:[/bold]\n\n"
        "  Preview what would be shown (no changes):\n"
        "    [cyan]clean-inbox cleanup --dry-run[/cyan]\n\n"
        "  Review all remaining senders and trash chosen ones:\n"
        "    [cyan]clean-inbox cleanup --trash[/cyan]\n\n"
        "  Full inbox sweep, permanently delete chosen senders:\n"
        "    [cyan]clean-inbox cleanup --all --delete[/cyan]"
    ),
)
def cleanup(
    config: ConfigOpt = None,
    dry_run: DryRunOpt = False,
    folder: FolderOpt = None,
    max_messages: MaxOpt = None,
    fetch_all: Annotated[bool, typer.Option("--all", help="Fetch every message in the folder, ignoring --max.")] = False,
    trash: Annotated[bool, typer.Option("--trash/--no-trash", help="Move chosen senders' messages to trash. [dim]Default: off[/dim]")] = False,
    delete: Annotated[bool, typer.Option("--delete", help="Permanently delete chosen senders' messages. Cannot be undone. [dim]Default: off[/dim]")] = False,
    reprocess: Annotated[bool, typer.Option("--reprocess", help="Show all senders again, ignoring the already-processed list.")] = False,
    log_file: Annotated[Path, typer.Option("--log-file", help="Path to log file. [dim]Default: clean-inbox.log[/dim]")] = DEFAULT_LOG_FILE,
) -> None:
    """\
    Bulk-delete transactional or notification emails by sender — no unsubscribe step.

    Use this after [cyan]scan[/cyan] has handled your marketing mail. Fetches all messages,
    groups every sender by message count, skips senders already processed or
    whitelisted, then lets you review and choose which ones to trash or delete.

    [bold]Interactive choices:[/bold]

      [green]y[/green]  delete this sender's messages (trash or permanently, per flags)
      [yellow]n[/yellow]  skip — leave this sender's messages alone
      [cyan]w[/cyan]  whitelist — never show this sender again, keep their messages
      [red]q[/red]  stop reviewing (already-approved senders are still acted on)

    [bold]Note:[/bold] No unsubscribe requests are sent. This command only removes messages.
    """
    cfg, config_path = load_config(config)
    if folder:
        cfg.folder = folder
    if fetch_all:
        cfg.max_messages = sys.maxsize
    elif max_messages:
        cfg.max_messages = max_messages

    _setup_logging(log_file, dry_run)

    console.rule(f"[bold]clean-inbox cleanup v{__version__}[/bold]")
    if dry_run:
        console.print(Panel("[yellow bold]DRY RUN MODE — no changes will be made[/yellow bold]", expand=False))
    console.print(f"[dim]Logging to {log_file}[/dim]")

    processed = set() if reprocess else load_processed(config_path, command="cleanup")
    whitelist = set(cfg.whitelist)

    # ------------------------------------------------------------------
    # 1. Fetch
    # ------------------------------------------------------------------
    console.print(f"\n[bold]Connecting to [cyan]{cfg.provider}[/cyan] → folder [cyan]{cfg.folder}[/cyan]...[/bold]")
    if reprocess:
        console.print("[dim]--reprocess: ignoring previously processed senders[/dim]")
    provider = _build_provider(cfg)

    fetch_label = "all" if fetch_all else f"up to {cfg.max_messages}"
    with provider:
        with console.status(f"Fetching {fetch_label} messages..."):
            messages = list(provider.fetch_messages(folder=cfg.folder, max_messages=cfg.max_messages))
    console.print(f"  Fetched [bold]{len(messages)}[/bold] messages.")

    # ------------------------------------------------------------------
    # 2. Group by sender, skip processed + whitelisted
    # ------------------------------------------------------------------
    grouped: dict[str, list[EmailMessage]] = defaultdict(list)
    for msg in messages:
        if msg.sender_address not in processed and msg.sender_address not in whitelist:
            grouped[msg.sender_address].append(msg)

    skipped = len(messages) - sum(len(v) for v in grouped.values())
    grouped = dict(sorted(grouped.items(), key=lambda kv: -len(kv[1])))

    console.print(
        f"  [bold]{len(grouped)}[/bold] sender(s) to review  "
        f"[dim]({skipped} message(s) skipped — already processed or whitelisted)[/dim]\n"
    )

    if not grouped:
        console.print("[green]Nothing left to review.[/green]")
        raise typer.Exit(0)

    # ------------------------------------------------------------------
    # 3. Render sender table
    # ------------------------------------------------------------------
    table = Table(title="Remaining Senders", box=box.ROUNDED, show_lines=True, expand=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Sender", min_width=35)
    table.add_column("Msgs", justify="right", width=6)
    table.add_column("Latest subject", min_width=40)

    for idx, (addr, msgs) in enumerate(grouped.items(), 1):
        latest = sorted(msgs, key=lambda m: m.date or m.message_id, reverse=True)[0]
        subject = latest.subject[:55] + ("…" if len(latest.subject) > 55 else "")
        table.add_row(str(idx), addr, str(len(msgs)), subject)

    console.print(table)

    # ------------------------------------------------------------------
    # 4. Interactive review
    # ------------------------------------------------------------------
    approved: set[str] = set()
    whitelisted_now: set[str] = set()

    console.print(
        "\n[bold]Review each sender:[/bold] "
        "y=delete messages  n=skip  w=whitelist (keep messages)  q=quit\n"
    )
    for addr, msgs in grouped.items():
        latest = sorted(msgs, key=lambda m: m.date or m.message_id, reverse=True)[0]
        example = latest.subject[:60] + ("…" if len(latest.subject) > 60 else "")
        console.print(
            f"  [bold]{addr}[/bold]  {len(msgs)} msg(s)\n"
            f"  e.g. [dim]\"{example}\"[/dim]"
        )
        choice = Prompt.ask("  Action", choices=["y", "n", "w", "q"], default="n")
        if choice == "q":
            console.print("[dim]Stopping review early.[/dim]")
            break
        elif choice == "y":
            approved.add(addr)
            save_processed_entry(addr, config_path, command="cleanup")
        elif choice == "w":
            whitelisted_now.add(addr)
            wl_file = save_whitelist_entry(addr, config_path)
            console.print(f"  [cyan]Whitelisted[/cyan] — saved to {wl_file}")
        console.print()

    if not approved:
        console.print("[dim]No senders selected for deletion. Done.[/dim]")
        raise typer.Exit(0)

    delete_messages = [msg for addr in approved for msg in grouped[addr]]
    total = len(delete_messages)

    # ------------------------------------------------------------------
    # 5. Trash or delete
    # ------------------------------------------------------------------
    actioned = 0
    if trash:
        if not dry_run:
            if Confirm.ask(f"\nMove [red]{total}[/red] message(s) to trash?"):
                provider = _build_provider(cfg)
                with provider:
                    with console.status(f"Moving {total} messages to trash..."):
                        for msg in delete_messages:
                            provider.move_to_trash(msg)
                            _log.info("CLEANUP TRASH  %-40s  subject=%r", msg.sender_address, msg.subject[:80])
                actioned = total
                console.print(f"[green]Moved {total} messages to trash.[/green]")
            else:
                console.print("[dim]Skipping trash.[/dim]")
        else:
            actioned = total
            for msg in delete_messages:
                _log.info("CLEANUP TRASH DRY-RUN  %-40s  subject=%r", msg.sender_address, msg.subject[:80])
            console.print(f"\n[yellow][dry-run] Would move {total} message(s) to trash.[/yellow]")

    elif delete:
        if not dry_run:
            if Confirm.ask(
                f"\n[bold red]Permanently delete {total} message(s)? This cannot be undone.[/bold red]"
            ):
                provider = _build_provider(cfg)
                with provider:
                    with console.status(f"Permanently deleting {total} messages..."):
                        for msg in delete_messages:
                            provider.delete_permanently(msg)
                            _log.info("CLEANUP DELETE %-40s  subject=%r", msg.sender_address, msg.subject[:80])
                actioned = total
                console.print(f"[green]Permanently deleted {total} messages.[/green]")
            else:
                console.print("[dim]Skipping deletion.[/dim]")
        else:
            actioned = total
            for msg in delete_messages:
                _log.info("CLEANUP DELETE DRY-RUN %-40s  subject=%r", msg.sender_address, msg.subject[:80])
            console.print(f"\n[yellow][dry-run] Would permanently delete {total} message(s).[/yellow]")
    else:
        console.print(
            f"\n[yellow]{total} message(s) selected but no action flag given. "
            "Use --trash or --delete to remove them.[/yellow]"
        )

    # ------------------------------------------------------------------
    # 6. Summary
    # ------------------------------------------------------------------
    summary = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    summary.add_column(style="dim")
    summary.add_column(justify="right", style="bold")

    summary.add_row("Emails fetched",       str(len(messages)))
    summary.add_row("Senders reviewed",     str(len(grouped)))
    summary.add_row("Senders approved",     str(len(approved)))
    summary.add_row("Senders whitelisted",  str(len(whitelisted_now)))
    summary.add_row("Senders skipped",      str(len(grouped) - len(approved) - len(whitelisted_now)))
    if trash:
        label = "Moved to trash (dry run)" if dry_run else "Moved to trash"
        summary.add_row(label, str(actioned))
    if delete:
        label = "Permanently deleted (dry run)" if dry_run else "Permanently deleted"
        summary.add_row(label, f"[red]{actioned}[/red]")

    console.print()
    console.print(Panel(summary, title="[bold]Summary[/bold]", expand=False))
    console.print("[bold green]Done.[/bold green]")
    _log.info("Cleanup complete — reviewed=%d approved=%d actioned=%d", len(grouped), len(approved), actioned)
    _log.info("")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
