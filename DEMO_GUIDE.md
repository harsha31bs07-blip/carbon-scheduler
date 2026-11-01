# Carbon Scheduler: local demonstration

Updated 2026-11-01. The local implementation is an A2 prototype. Human semantic
reviews, a second real destination and final live experimental evidence remain
release gates. Historical scenarios and projected savings are not current
measurement claims. The prior guide is preserved in
[the historical copy](docs/plans/DEMO_GUIDE.pre-A2-2026-11-01.md).

## Start locally

Use [the Windows setup guide](docs/development.md), then run from PowerShell:
Use that environment's Python (for example `.\.venv-a2\Scripts\python.exe`)
where the commands below say `python`.

```powershell
$env:PYTHON_DOTENV_DISABLED = '1'
$env:OTP_DEV_MODE = 'true'
python -m streamlit run workflow/dashboard/app.py --server.address 127.0.0.1
```

Use an email address in the local login screen. Development mode displays the
verification code without SMTP. Enter it to open **Ticket workflows**. This
login is intended for the loopback-bound local development app.

The page and worker are separate. A queued ticket starts no model calls during
Streamlit reruns. If Ollama is installed, start its service in another terminal:

```powershell
ollama serve
python -m workflow.deployment_inventory --config config/deployments.local.json
```

The November 1 rehearsal found the local endpoint unavailable, so no generation
was attempted. Configured model names are not observed availability. Missing
models are not downloaded implicitly.

## Demonstrate the implementation

1. **Fixed assignment:** queue a fictional ticket, choose an installed model and
   destination, deadlines and optional summary delay. Run the exact worker
   command displayed for that database. Refresh to inspect actual UTC times,
   executed identities and durable outputs.
2. **Optimized policy:** choose speed/balanced/eco/custom and supply frozen
   profiles, size forecasts and carbon inputs. Dashboard submission retains
   unobserved health; the worker verifies destinations. Current real profiles
   are unreviewed, so demonstrate honest infeasibility and rejection/context
   details. Never weaken a requirement to manufacture eligibility.
3. **Accounting:** inspect complete/unknown totals, partial subtotals, provenance
   and coverage. Zero API fees exclude electricity and ownership. Annual CO2
   proxies do not demonstrate hourly deferral savings.
4. **Comparison evidence:** load a validated evaluation or portable comparison.
   Inspect all outcomes, paired differences and quality coverage. A prepared
   pilot with zero completed trials is not experimental results. Simulation
   stays explicitly labeled.
5. **Human review:** select a completed naturally stopped trial in an original
   evaluation with a frozen rubric. Read the ticket, KB, prompts and outputs.
   Only a human supplies criterion scores/rationales and explicitly approves.
   Blank fields supply no quality. Existing approvals cannot be overwritten.

Historical workflow replay remains available offline:

```powershell
python -m workflow.queue_replay inspect --bundle results/demo/a2-local-restart-20261019
python -m workflow.queue_replay inspect --bundle results/demo/a2-planned-infeasible-20261026
```

The first bundle demonstrates fixed live durability; the second retains honest
planned infeasibility. Neither is a final matched five-metric experiment.

## Completed offline example

With no inference host available, open **Comparison evidence**, choose **Portable
comparison**, and load `results/benchmarks/a2-offline-dashboard-20261101`.
It contains four completed scripted trials with simulation labels, unknown
quality/energy/carbon and no release eligibility. To practice the human-review
form, load its original local evaluation directory
`.runtime/p7-offline-evaluation-20261101` instead. Practice judgments remain
simulation evidence and do not review the real calibration outputs.

Regenerate this example into new directories with:

```powershell
python -m workflow.demo_evidence --output path/to/new-portable-demo --work-directory path/to/new-demo-evaluation
```

This command uses scripted outputs and a simulated clock. It never calls a
provider, downloads a model or supplies human scores.

## Complete the experimental release

Human judgments, profiles covering actual contexts, a calibrated second host and
suitable timestamped carbon data are still needed. Freeze independent final
inputs, run matched live trials and preserve failed/equal/worse outcomes. See
[policy evaluation](docs/policy-evaluation.md),
[workflow reviews](docs/workflow-quality-review.md) and
[the completion plan](docs/plans/COMPLETION_PLAN-2026-10-26.md).
