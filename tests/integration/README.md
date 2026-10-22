# Salesforce live proof is not implemented here

The current test is an always-inert placeholder. It imports no CRM runtime, reads no configuration or credential, and performs no network or account action. Setting an opt-in environment variable cannot activate it. One skipped test is not evidence of a real Salesforce connection or write.

Offline readiness behavior is tested in `tests/adapters/crm/test_readiness.py` with explicitly fictional operator declarations. Positive reports check their closed shape, config binding, self-consistency, time and stated action stage. They do not authenticate those declarations or authorize execution.

A later reviewed control increment must enforce fresh exact read/write authority and compare every send declaration against the actual audited immutable original-bound intent before using the unchanged writer. It must preserve one-attempt admission, captured source versions and absorbing unknown holds. A separately authorized first qualification send must never be treated as an ordinary qualified write.

Real proof requires the owner's isolated Developer Edition account, reviewed app/permissions/field/record actions, the actual supported API version and selected schema, expected identity, existing n8n runtime/network, and a concrete private authority manifest. The owner handles sensitive entry. Production targets, unlisted records, ambient credentials, missing authority, or unqualified ordinary writes must refuse even if credentials happen to exist.

Run the inert placeholder with the project's qualified private interpreter and existing dependencies. Do not launch the rejected virtualenv interpreter or install anything. Before adding a live body, obtain the separate scoped technical review and the concrete external action authorization described in `integrations/salesforce/SETUP.md`.
