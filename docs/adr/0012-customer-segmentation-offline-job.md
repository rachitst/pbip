# ADR 0012: Customer segments are assigned by an offline K-Means job; the API only reads them
- Status: Accepted
- Date: 2026-10-29
- Deciders: Architecture Team

## Context
TASK-044 clusters customers on RFM (recency, frequency, monetary) and lifestyle features into
explainable segments and persists the labels. The first implementation imported the training
pipeline into the request path, and `GET /api/v1/analytics/segment` fell back to a hard-coded
rule ladder with a fabricated confidence of 0.85 that it then committed, so a stored row could
not be told apart from a model-produced one. The batch job crashed on an empty database and the
runner masked any failure as "database unavailable" and exited 0.

## Decision
* **Features** (`analytics/repository.py`): per customer, over POSTED non-REVERSAL entries of
  all their accounts: recency (days since the last entry), frequency (debits per 30-day month),
  monetary (spend per 30-day month), savings ratio ((credits - debits) / credits) and the
  discretionary share of spend (dining, shopping, entertainment, travel). A customer without any
  ledger entry has no features and gets no segment; nothing is invented.
* **Training stays out of the request path.** `pbip_ml.segmentation.pipeline` (K-Means with a
  fixed seed on standardised features, Silhouette and Davies-Bouldin reported) is imported only by
  `analytics/segmentation_job.py`, which `scripts/run_segmentation.py` runs. Router and service
  import the contract only. `n_clusters` is capped at the five personas, each cluster is named
  from its centroid in a fixed priority order, and metrics that cannot be computed are `null`.
* **Persistence:** `customer_segments` (migration `0004`), one row per customer, written by the
  job with `ON CONFLICT` upserts and one commit; re-runs update in place. The job skips cleanly
  (reported, exit 0, nothing written) with fewer than two eligible customers; any other failure
  propagates and the script exits non-zero.
* **Read path:** `GET /api/v1/analytics/segment` returns the principal's own stored row or 404
  until the job has run. It never writes.

## Alternatives considered
- Rule-based fallback on first read: not K-Means (AC 1), writes on a GET, and fabricates a
  confidence; rejected.
- Celery beat schedule for the job: deferred; the job is deterministic and cheap to run by hand
  or from CI, and the blueprint's nightly analytics slot can host it once its cadence is decided.

## Consequences
- Segments are as fresh as the last job run; `assigned_at` says when.
- Quality is reported, not gated: a poor Silhouette score is visible in the report and the job
  still persists labels. A gate needs a baseline from real data first.
