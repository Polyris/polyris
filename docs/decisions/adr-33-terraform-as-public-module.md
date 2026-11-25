# ADR #33 — Terraform as a Public Module

**Context:** `terraform/shared/` was an internal directory not intended for direct use.

**Decision:** `sam/` — a public module with README, inputs/outputs documentation. `sam/` — example root module for users.

**Reasons:**
- Standard Terraform module pattern
- Users can consume the module directly via GitHub source
- Clear separation between module (reusable) and example (root)

**Note:** Superseded by ADR #34 (AWS SAM replaces OpenTofu for shared infrastructure).
