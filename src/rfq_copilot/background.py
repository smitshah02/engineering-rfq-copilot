"""Process lifecycle only; claims, fencing and retry budgets belong to the queue consumers."""

import argparse
import asyncio
import logging
import signal
from contextlib import contextmanager

from rfq_copilot.database import DatabaseError, PrivilegedRuntimeRole


def validate_settings(settings) -> None:
    # The small lower bound preserves injected fast settings in offline/isolated DB tests.
    # The executable's operator-facing lower bound is one second.
    if not 0.05 <= settings.poll_seconds <= 60:
        raise ValueError("poll_seconds must be 0.05..60")
    if not settings.poll_seconds <= settings.error_backoff_max_seconds <= 300:
        raise ValueError("error_backoff_max_seconds must be poll_seconds..300")
    if not 0 <= settings.shutdown_grace_seconds <= 300:
        raise ValueError("shutdown_grace_seconds must be 0..300")


def arguments(parser: argparse.ArgumentParser, environ, prefix: str, argv=None):
    parser.add_argument("--target", choices=("development", "test"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="claim at most one batch, then exit")
    mode.add_argument(
        "--continuous",
        action="store_true",
        help="poll until SIGINT/SIGTERM (default)",
    )
    parser.add_argument(
        "--poll-seconds",
        default=environ.get(f"{prefix}_POLL_SECONDS", "5"),
        help="queue polling interval, 1..60 seconds (default 5)",
    )
    parser.add_argument(
        "--error-backoff-max-seconds",
        default=environ.get(f"{prefix}_ERROR_BACKOFF_MAX_SECONDS", "60"),
        help="database error backoff cap, poll interval..300 seconds (default 60)",
    )
    parser.add_argument(
        "--shutdown-grace-seconds",
        default=environ.get(f"{prefix}_SHUTDOWN_GRACE_SECONDS", "30"),
        help="finish in-flight work for 0..300 seconds before cancellation (default 30)",
    )
    args = parser.parse_args(argv)
    return args


def settings_from_args(parser, args, environ, prefix: str, settings_type):
    if not args.once and args.target is None:
        parser.error("continuous processing requires --target development or --target test")
    args.target = args.target or "development"  # retain the historical --once default
    try:
        settings = settings_type(
            concurrency=int(environ.get(f"{prefix}_CONCURRENCY", "1")),
            poll_seconds=float(args.poll_seconds),
            error_backoff_max_seconds=float(args.error_backoff_max_seconds),
            shutdown_grace_seconds=float(args.shutdown_grace_seconds),
        )
        if settings.poll_seconds < 1:
            raise ValueError
    except (ValueError, TypeError):
        parser.error(
            "invalid background settings: poll 1..60, backoff poll..300, "
            "grace 0..300, concurrency 1..4"
        )
    return settings


@contextmanager
def signals(stopping: asyncio.Event):
    loop = asyncio.get_running_loop()
    installed = []
    previous = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.getsignal(sig)
            loop.add_signal_handler(sig, stopping.set)
            installed.append(sig)
        yield
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
            signal.signal(sig, previous[sig])


async def pause(stopping: asyncio.Event, seconds: float) -> None:
    """Interruptible even during an empty queue or capped outage backoff."""
    try:
        await asyncio.wait_for(stopping.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def drain(active, grace: float) -> list:
    if not active:
        return []
    _, pending = await asyncio.wait(active, timeout=grace)
    for task in pending:
        task.cancel()
    # Await transaction rollback / fenced release before disposing the database pool.
    return await asyncio.gather(*active, return_exceptions=True)


async def run_once(consumer, claim) -> int:
    """Preserve one-batch semantics while retaining custody if a later claim fails."""
    claims = []
    try:
        for _ in range(consumer.settings.concurrency):
            if consumer.stopping.is_set():
                break
            run = await claim(consumer.database, consumer.settings)
            if run is None:
                break
            claims.append(run)
    except BaseException:
        consumer.stopping.set()
        await drain(
            {asyncio.create_task(consumer._track(run)) for run in claims},
            consumer.settings.shutdown_grace_seconds,
        )
        raise
    # Finish claiming the batch before starting it, as the historical --once did.
    active = {asyncio.create_task(consumer._track(run)) for run in claims}
    try:
        await asyncio.gather(*active)
        return len(active)
    finally:
        if any(not task.done() for task in active):
            consumer.stopping.set()
            await drain(active, consumer.settings.shutdown_grace_seconds)


async def run(consumer, *, once: bool) -> None:
    with signals(consumer.stopping):
        if not once:
            await consumer.serve()
            return
        task = asyncio.create_task(consumer.run_once())
        stop = asyncio.create_task(consumer.stopping.wait())
        try:
            done, _ = await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
            else:
                results = await drain({task}, consumer.settings.shutdown_grace_seconds)
                for result in results:
                    if isinstance(result, Exception):
                        raise result
        finally:
            stop.cancel()
            if not task.done():
                consumer.stopping.set()
                task.cancel()
            await asyncio.gather(stop, task, return_exceptions=True)


async def serve(consumer, claim, logger: logging.Logger) -> None:
    """Fill available slots once per poll; never construct a database/engine in this loop.

    A failed claim may have committed: leave it to the existing lease/fence recovery.
    Unsettled task failures follow the same rule. Connection recovery has no job-attempt
    budget; only a successful queue claim spends an existing bounded attempt.
    """
    active: set[asyncio.Task] = set()
    delay = consumer.settings.poll_seconds
    logger.info("event=consumer_started mode=continuous")
    try:
        while not consumer.stopping.is_set():
            unavailable = False
            for task in list(active):
                if task.done():
                    active.remove(task)
                    try:
                        task.result()
                    except PrivilegedRuntimeRole:
                        raise  # security configuration is never a transient outage
                    except DatabaseError:
                        unavailable = True
                        logger.warning("event=settlement_unavailable recovery=lease")
            if not unavailable:
                claims = []
                try:
                    for _ in range(consumer.settings.concurrency - len(active)):
                        if consumer.stopping.is_set():
                            break
                        try:
                            run = await claim(consumer.database, consumer.settings)
                        except PrivilegedRuntimeRole:
                            raise
                        except DatabaseError:
                            unavailable = True
                            logger.warning("event=queue_unavailable recovery=lease")
                            break
                        if run is None:
                            break
                        claims.append(run)
                finally:
                    # Start after the batch of claims so an immediate technical failure
                    # cannot be reclaimed in this same polling round. Retain every known
                    # claim even if a later claim failed or a signal arrived meanwhile.
                    for run in claims:
                        active.add(asyncio.create_task(consumer._track(run)))
                        logger.info("event=work_claimed")
            if unavailable:
                logger.warning("event=consumer_backoff delay_seconds=%s", delay)
                await pause(consumer.stopping, delay)
                delay = min(delay * 2, consumer.settings.error_backoff_max_seconds)
            else:
                delay = consumer.settings.poll_seconds
                await pause(consumer.stopping, delay)
    finally:
        consumer.stopping.set()
        logger.info("event=consumer_stopping")
        results = await drain(active, consumer.settings.shutdown_grace_seconds)
        for result in results:
            if isinstance(result, DatabaseError):
                logger.warning("event=shutdown_unsettled recovery=lease")
        logger.info("event=consumer_stopped")
