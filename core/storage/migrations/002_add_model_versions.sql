-- Migration 002: Supplementary indexes for model_versions and performance optimization
-- The model_versions table is already in 001_initial.sql, so this adds additional indexes

-- Add composite index for efficient lookups of deployed models
CREATE INDEX IF NOT EXISTS idx_model_versions_deployed_name ON model_versions(deployed_at, model_name)
WHERE deployed_at IS NOT NULL AND retired_at IS NULL;

-- Add index for version lookup
CREATE INDEX IF NOT EXISTS idx_model_versions_name_version ON model_versions(model_name, version);
