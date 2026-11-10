"""python -m gate check|release|finish"""

import argparse
import json
import signal
import sys

from gate.decision import Decision, Policy, Verdict
from gate.evidence import EvidenceCollector
from gate.prometheus import Prometheus
from gate.release import Status, evaluate, run_release

EXIT_CODES = {Status.PROMOTED: 0, Status.ROLLED_BACK: 1, Status.STUCK: 2}
HOLD_EXIT = 3


class Interrupted(Exception):
    pass


def _interrupt(signum, _frame):
    raise Interrupted(signal.Signals(signum).name)


def _print(event: dict) -> None:
    print(json.dumps(event), flush=True)


def _deployer(args):
    import boto3

    from gate.deployer import AlbWeights

    client = boto3.client("elbv2", region_name=args.region)
    return AlbWeights(client, args.listener_arn, args.stable_group_arn, args.canary_group_arn)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gate")
    commands = parser.add_subparsers(dest="command", required=True)

    def add_prometheus(sub):
        sub.add_argument("--prometheus", required=True)
        sub.add_argument("--version", required=True)

    def add_load_balancer(sub):
        sub.add_argument("--region", required=True)
        sub.add_argument("--listener-arn", required=True)
        sub.add_argument("--stable-group-arn", required=True)
        sub.add_argument("--canary-group-arn", required=True)

    add_prometheus(commands.add_parser("check", help="judge a canary once"))
    release = commands.add_parser("release", help="walk the canary through its traffic steps")
    add_prometheus(release)
    add_load_balancer(release)
    add_load_balancer(commands.add_parser("finish", help="send all traffic back to stable"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    policy = Policy()

    if args.command == "finish":
        _deployer(args).set_canary_weight(0)
        _print({"event": "finished", "canary_weight": 0})
        return 0

    collector = EvidenceCollector(Prometheus(args.prometheus), args.version, policy.window)

    if args.command == "check":
        decision = evaluate(collector.collect, policy)
        _print({"verdict": decision.verdict.value, "reason": decision.reason})
        return (
            0
            if decision.verdict is Verdict.PROMOTE
            else (1 if decision.verdict is Verdict.ROLLBACK else HOLD_EXIT)
        )

    signal.signal(signal.SIGTERM, _interrupt)
    signal.signal(signal.SIGINT, _interrupt)

    def report(step: int, decision: Decision) -> None:
        _print({"step": step, "verdict": decision.verdict.value, "reason": decision.reason})

    outcome = run_release(_deployer(args), collector.collect, policy, report=report)
    _print({"status": outcome.status.value, "reason": outcome.reason})
    return EXIT_CODES[outcome.status]


if __name__ == "__main__":
    sys.exit(main())
