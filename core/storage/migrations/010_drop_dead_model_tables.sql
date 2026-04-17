-- Migration 010: drop unused ML model tables.
--
-- The core/models/ subsystem and scripts/train_models.py CLI were
-- unwired scaffolding — no live code reads or writes these tables.
-- Live strategy labels (P1-P5) are applied by core.strategies.assignment
-- via spread buckets, not ML predictions.

DROP TABLE IF EXISTS model_predictions;
DROP TABLE IF EXISTS model_evaluations;
DROP TABLE IF EXISTS model_versions;
