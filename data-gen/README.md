# pbip-datagen - synthetic banking data (v1)

Deterministic generator for ML/analytics work. **Synthetic data only** - never loaded into the
operational ledger (see `docs/adr/0009-synthetic-dataset-schema.md`).

## What v1 generates

| Persona | Income pattern | Notes |
|---|---|---|
| salaried_professional | salary on the last business day; April appraisal | most likely to carry an EMI |
| freelancer | 1-4 lumpy client payments per month | income varies month to month |
| student | family allowance in the first days of the month | high dining/entertainment share |
| retiree | pension on the first business day | utility-heavy, low activity |
| business_owner | 2-6 variable customer receipts | travel/shopping heavy |

Calendar events: monthly salary/pension/allowance credits, fixed EMI debits on the customer's EMI
day (moved to the next business day at weekends), electricity bills with an Indian-summer spike
(May ~1.6x), mobile bills, festive-season (Oct-Dec) uplift in discretionary spending, and
persona-specific churn (customers go silent after `churned_on`). Money is integer paise.

**Determinism:** the same `(seed, customers, months, start)` gives byte-identical output; customer
`i` is identical regardless of population size; ids are UUIDv5, so reloads replace a run.

## Usage

```bash
python scripts/dev.py migrate                        # creates the `synthetic` schema
python scripts/dev.py datagen                        # seed 42, 1,000 customers, 12 months -> PostgreSQL (COPY)
python scripts/dev.py datagen --seed 7 --customers 200 --months 24
python scripts/dev.py datagen export --out data-gen/output   # CSV files instead (git-ignored)
```

`make datagen`, `.\dev.ps1 datagen` and `dev.cmd datagen` run the same command. In Python:
`from pbip_datagen.generator import GeneratorConfig, generate`.

## Limitations (stated honestly)

Distributions are hand-specified, not fitted to real bank data; no public-holiday calendar; no
fraud labels in v1 (data-gen v2 injects F1-F6); a model trained only on this data can learn the
generator's rules - evaluation must use held-out scenario variants (blueprint section 9).
