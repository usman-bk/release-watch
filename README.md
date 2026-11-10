# release-watch

_Built with AI assistance._

Ships a small API to AWS with Terraform, measures it with Prometheus, and lets a new
release take live traffic only as far as the numbers allow.

Terraform, AWS (ECS Fargate, ALB, EC2, Cloud Map), Prometheus, Alertmanager, Grafana,
Python, FastAPI. The rules, routes and dashboard are tested in the real programs.

```
app/                      the quote API and its metrics
gate/decision.py          promote, hold or roll back, from what was measured
gate/evidence.py          the PromQL behind that decision
gate/release.py           walks the traffic steps and always ends somewhere safe
monitoring/prometheus/    scrape config, SLO rules and their promtool tests
monitoring/grafana/       the provisioned dashboard
infra/                    network, service and monitoring modules, with tests
tests/integration/        real Prometheus, Alertmanager and Grafana as processes
```

## What this has and has not proven

**This has never been applied to a real AWS account.** Terraform validates against the
real AWS provider schema, and its tests run on a mocked provider. That proves the
configuration is coherent and that its safety properties hold. It does not prove AWS
will accept every value, or that the monitoring host boots.

| Part | How it was checked |
| ---- | ------------------ |
| Service, gate | Real requests, real scrapes, the real Prometheus API |
| Alert rules | `promtool` over simulated months, then a real outage through Alertmanager |
| Dashboard | Every panel's query run through a real Grafana |
| Images | Built and started on ARM in CI |
| AWS API call that moves traffic | Validated against the AWS API model by botocore's stubber |
| Terraform | `validate`, `terraform test` with mocks, tflint, Trivy |
| Boot script | Its disk and secret functions, with system commands faked |

## Three answers, not two

Each traffic step ends in one of three verdicts:

| Verdict  | When | What happens |
| -------- | ---- | ------------ |
| promote  | The canary is confidently inside both limits | The next step |
| roll back | The canary is confidently outside a limit | Weight goes to 0 |
| hold     | Anything else | Ask again, then roll back at the deadline |

Hold is most of the work. These all hold, and none can promote: Prometheus not
answering, a canary target down, measurements older than three scrapes, too few
requests, and a stable track that is failing too. In that last case a canary matching a
sick stable has shown nothing, while one far worse than it still rolls back.

"Confidently" is a 95% Wilson interval. Two errors in 250 requests is 0.8%, and over
the limit if you take it at face value, but the interval says it could be noise, so the
gate waits. The same 0.8% over 100,000 requests rolls back at once.

Running out of time counts as a failure. If the gate cannot tell by the deadline, traffic
goes back to stable, because no evidence is not good evidence. Any crash or SIGTERM
during a release does the same. If traffic cannot be moved back after four tries, the
gate exits 2 and says a person is needed.

## Running a release

```sh
terraform apply -var canary_tag=v2                  # starts canary tasks at 0% traffic
python -m gate release --prometheus URL --version v2 \
  --region eu-central-1 --listener-arn ... \
  --stable-group-arn ... --canary-group-arn ...     # 10%, 25%, 50%, or back to 0
terraform apply -var stable_tag=v2 -var canary_tag= # stable takes the new image
python -m gate finish --region ... --listener-arn ... # all traffic to stable
```

Exit codes: 0 promoted, 1 rolled back, 2 stuck, and 3 from `check` for a hold.

## Alerts

Multiwindow burn rates from the Google SRE Workbook, chapter 5, against a 99.9%
availability objective and 99% of quotes under 300 ms.

| Burn | Long / short window | Spends | Goes to |
| ---- | ------------------- | ------ | ------- |
| 14.4 | 1h / 5m | 2% of the month in an hour | page |
| 6    | 6h / 30m | 5% in six hours | page |
| 3    | 1d / 2h | 10% in a day | ticket |
| 1    | 3d / 6h | 10% in three days | ticket |

The long window shows it matters, and the short one lets the alert clear within minutes
of recovery. Tickets are inhibited while a page is firing for the same job. A `Watchdog`
alert always fires and goes to a heartbeat receiver, which is the only way to find out
that the monitoring itself has died.

## Traps it handles

- **An apply in the middle of a release.** Terraform owns the listener but ignores its
  weights, or the next apply would silently send everything back to stable.
- **An apply on a scaled-up service.** Same idea for the stable task count, which
  autoscaling owns.
- **The first failures of a release.** A counter that first appears at 1 has no earlier
  sample, so `increase()` misses it. Error series are published at zero on start up.
- **Prometheus on EFS.** Its docs say NFS is not supported, so metrics live on an EBS
  volume that outlives the host, and the boot script only formats a blank disk.
- **Retention.** The default is 15 days and the error budget window is 30, so it is set
  to 35.
- **Secrets.** The Grafana password is write-only, so it never enters the Terraform
  state. The instance metadata hop limit keeps the host's AWS role out of the containers.

## Running it

```sh
./scripts/fetch-tools.sh    # exact versions, checked against published SHA-256
uv sync
uv run pytest
.tools/prometheus/promtool test rules monitoring/prometheus/tests/slo_test.yml
cd infra && terraform init -backend=false && terraform test
```

CI runs all of this, then builds every image and starts it on ARM, the way the ECS task
and the monitoring host start it.

## Limits

- Never applied to AWS, as above. The DNS discovery in `prometheus.yml` has only been
  checked for syntax and for matching the names Terraform registers.
- The monitoring host is one instance in one zone. The heartbeat tells you it is gone,
  and it does not bring it back.
- The interval assumes requests fail independently. A failure that hits one customer's
  retries repeatedly looks more certain than it is.
- Each task runs one Uvicorn process, because the metrics client is per process.
- Nothing stops two releases running at once. Run them from one pipeline.
- Whatever runs the gate has to reach Prometheus, which is private: inside the VPC or
  through an SSM port forward.
