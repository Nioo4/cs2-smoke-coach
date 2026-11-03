"""Dry-run by default; never infer an isolated database from production settings."""

import argparse
import asyncio
import json
import os
import sys

from sqlalchemy.engine import make_url

from app.services import tactics_maintenance as maintenance


def run_async(coroutine):
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=factory) as runner:
        return runner.run(coroutine)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["cleanup", "prepare-restore"])
    parser.add_argument(
        "--database-url", default=os.environ.get("TACTICS_MAINTENANCE_DATABASE_URL")
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-database")
    parser.add_argument("--container-target", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-batches", type=int, default=10)
    args = parser.parse_args(argv)
    try:
        if not args.database_url:
            raise maintenance.MaintenanceError("MAINTENANCE_EXPLICIT_URL_REQUIRED")
        options = {
            "execute": args.execute,
            "confirm_database": args.confirm_database,
            "container": args.container_target,
        }
        if args.command == "cleanup":
            result = run_async(
                maintenance.cleanup(
                    args.database_url,
                    args.project,
                    args.run_id,
                    batch_size=args.batch_size,
                    max_batches=args.max_batches,
                    **options,
                )
            )
        else:
            result = run_async(
                maintenance.prepare_restore(args.database_url, args.project, args.run_id, **options)
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except maintenance.MaintenanceError as exc:
        output = {"error": str(exc), "completed": False, "execute_requested": args.execute}
        if "REMAINS_CLOSED" in str(exc):
            database = make_url(args.database_url).database
            output.update(
                database_closed=True,
                database=database,
                project=args.project,
                run_id=args.run_id,
                recovery_instruction="Do not start services. From the postgres "
                "control connection inspect this NEW target datallowconn and COMMENT project/run; "
                "stop all old API/Node connections. Only the operator may explicitly "
                "ALTER DATABASE "
                + maintenance._database_name(database)
                + " ALLOW_CONNECTIONS true to retry preparation. "
                "Never run that command against an old/production target; "
                "the tool does not reopen.",
            )
        print(json.dumps(output, sort_keys=True), file=sys.stderr)
        return 2
    except Exception:
        print(
            json.dumps(
                {
                    "error": "MAINTENANCE_FAILED",
                    "completed": False,
                    "execute_requested": args.execute,
                }
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
