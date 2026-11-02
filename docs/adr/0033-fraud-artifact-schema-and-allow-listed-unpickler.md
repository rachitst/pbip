# ADR 0033: Inference-safe fraud artifact module and an allow-listed unpickler
- Status: Accepted (under owner's standing approval, 2026-11-03)
- Date: 2026-11-03
- Deciders: Architecture Team

## Context
#350: with `FRAUD_SCORER=hybrid` (ADR-0019, the local/demo setting) the API imported
`pbip_ml.fraud.training` on the request path. `inference.py` imported `AnomalyModel` from it and
`hybrid.py` imported `MODEL_FEATURES` and `AnomalyModel` from it. AGENTS.md section 2 says training
code never runs on the request path and the API depends only on contracts and inference modules;
`training.py`'s own docstring says "never imported on the request path".

Moving the two names is easy. The difficulty is the served artifact: `isolation_forest.joblib` is a
pickle, and a pickle names each class by its import path. Every artifact trained so far (the local
`models/fraud` directory, and any version in the MLflow registry) names
`pbip_ml.fraud.training.AnomalyModel`, so `joblib.load` would import the training module again to
unpickle it, whatever the code imports.

Separately, threat T34 (tampered model artifact): `joblib.load` constructs any importable object a
pickle names, so the SHA-256 manifest and the `isinstance` check run too late to stop a pickle that
calls `os.system`. The manifest refuses corrupt, partial and edited files, but someone who can write
the directory can rewrite the manifest too.

Options considered for the existing artifacts:

1. **Re-save every artifact with the new class path.** Needs every copy found and rewritten: the
   untracked local directories and every registry version. Registry versions carry the SHA-256 the
   promotion gate measured (TASK-038), so rewriting them breaks `export-champion`'s check, or needs
   the gate re-run. A copy missed anywhere silently re-imports training. Rejected.
2. **Leave a shim class in `training.py`.** Unpickling would still import the training module.
   Does not fix #350. Rejected.
3. **A restricted unpickler that maps the one legacy class path and allow-lists the classes it may
   construct.** Old and new artifacts both load, nothing has to be rewritten, and a pickle naming
   any other callable is refused before it runs. Chosen.

## Decision
1. **New module `pbip_ml.fraud.artifacts`** (inference-safe: no training code, no `sklearn.metrics`
   import of its own) holds `MODEL_FEATURES`, `ANOMALY_FEATURES` and `AnomalyModel`, unchanged.
   `training.py` imports and re-exports them, so the training jobs, reports, scripts and tests that
   use `training.MODEL_FEATURES` keep working. `inference.py` and `hybrid.py` import from
   `artifacts`.
2. **`artifacts.load_anomaly_model`** reads `isolation_forest.joblib` with a subclass of joblib's own
   `NumpyUnpickler` (so the numpy arrays joblib stores inline are read exactly as `joblib.load`
   reads them) whose `find_class` accepts only `ALLOWED_CLASSES`: `AnomalyModel`,
   `joblib.numpy_pickle.NumpyArrayWrapper`, `numpy.dtype`, `numpy.ndarray`,
   `sklearn.ensemble._iforest.IsolationForest`, `sklearn.tree._classes.ExtraTreeRegressor` and
   `sklearn.tree._tree.Tree`. That is exactly the set the trained artifact uses (recorded from the
   real artifact with scikit-learn 1.9.1, numpy 2.5.4, joblib 1.6.0). Anything else raises
   `pickle.UnpicklingError`, which `inference.load` turns into `ModelLoadError`, and the API serves
   the rules alone (fail safe, ADR-0019).
3. **Legacy mapping:** `pbip_ml.fraud.training.AnomalyModel` is mapped to
   `pbip_ml.fraud.artifacts.AnomalyModel` inside `find_class`, before the allow-list check, so the
   training module is never imported. New artifacts name the new path.
4. **Import-linter contract** "fraud serving must not depend on fraud training": `pbip_api` and the
   serving modules (`artifacts`, `contract`, `features`, `hybrid`, `inference`, `rules`) may not
   import `pbip_ml.fraud.training`, `train`, `evaluation` or `promotion`, directly or indirectly.
   It reports BROKEN on the code before this change.
5. **No retraining, no metric change.** The model code is moved, not changed. Checked by training
   the seeded pipeline (seed 42, 600 customers, 4 months) on the code before and after: `lightgbm.txt`
   and `hybrid_params.json` are byte-identical, `report.json` differs only in the wall-clock latency
   fields, and the two Isolation Forests have identical `low`/`high`/medians/spreads and identical
   tree thresholds and split features. The only difference in `isolation_forest.joblib` is the class
   path it names.

## Consequences
* An API in hybrid mode no longer imports any fraud training job
  (`test_hybrid_scorer_loads_a_legacy_artifact_without_importing_fraud_training`, run in a clean
  subprocess from an empty directory). scikit-learn itself still loads, because the served model is
  a scikit-learn Isolation Forest, and scikit-learn's own package imports include `sklearn.metrics`;
  that is a dependency of serving the model, not of PBIP training code.
* Existing artifacts in `models/fraud` and the registry load unchanged; checked locally with the
  1 Nov 2026 `models/fraud` artifact (loads as `hybrid-lgbm-iforest-1`, training not imported).
* T34 is narrowed: a tampered pickle can no longer run an arbitrary callable at load
  (`test_a_pickle_that_names_a_class_outside_the_allow_list_is_refused_before_it_runs`). It does not
  make the directory trustworthy: the allowed classes still accept attacker-chosen state, so a
  writer could ship a forest that scores fraud low. The read-only directory, the manifest, the
  promotion gate and the rule floors stay the controls for that.
* **Maintenance cost:** a scikit-learn, numpy or joblib upgrade that renames a pickled class makes
  the load fail closed (rules only, `fraud_model_not_loaded` in the log), not open.
  `test_training_writes_a_manifest_that_load_verifies` trains and loads a real artifact, so such an
  upgrade fails CI first; the fix is a reviewed addition to `ALLOWED_CLASSES`.
* `artifacts.py` subclasses joblib's `NumpyUnpickler`, a public class in `joblib.numpy_pickle`
  but not part of joblib's documented API. An incompatible joblib change would also fail closed and
  be caught by the same test.
* No contract (`contract.py`), API, schema, migration or new dependency change.
