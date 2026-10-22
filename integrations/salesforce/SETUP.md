# Salesforce test account setup

This integration targets one isolated Developer Edition organization and at most three fictional Account records. The code has local injected-transport evidence. It has not yet proved a real Salesforce connection, field permissions, conditional update, or first write. HubSpot follows Salesforce acceptance.

The readiness validator checks supplied declarations against a pinned local configuration. A passing report does not authenticate the declarations, verify the actual saved write intent, grant account access, or authorize a request. The current live-test placeholder always skips, even when opt-in variables are set.

## 1. Finish signup and prepare a concrete setup request

The owner completes signup, legal acceptance, and sensitive entry. Do not paste passwords, tokens, client secrets, OAuth codes, or complete authorization URLs into chat, source, commands, logs, manifests, or evidence packets.

Before changing the account, review its actual organization/user IDs, My Domain and API/login/identity origins, supported API version, app/client identity, OAuth policy, user/profile/permission set, field schema and permissions, and the exact fictional record IDs. Also name the existing n8n runtime/version, approved workflow export, network path, and private local configuration/storage references. Missing facts stay missing. Do not invent a version, buy a license, install a runtime, create fields, grant access, or activate a workflow to make a check pass.

Use a private copy of `readiness-manifest.template.json`. Its null/empty facts deliberately cannot qualify anything. Evidence references are redacted hashes and times; they are operator declarations, not provider attestations. Keep the completed manifest private. The immutable API accepts its parsed document through `ReadinessManifest.from_document(...)`; the stored representation is canonical JSON.

## 2. Review the app and permissions before changing them

The proposed local flow uses an External Client App, PKCE S256, and only `api` and `refresh_token` scopes. Match the actual public/confidential classification and code/refresh secret requirements to the reviewed configuration. The exact existing callback is:

`http://localhost:8768/api/internal/crm/oauth/salesforce/callback`

The [Salesforce secure app guide](https://developer.salesforce.com/docs/platform/isvforce/guide/secure-code-ac-eca.html) explains PKCE and public/confidential exchange requirements. Its partner-production policies are not evidence of this test installation's settings. The [Data Loader PKCE guide](https://developer.salesforce.com/docs/platform/data-loader-impl/guide/loader-eca-oauth-pkce.html) describes that product's flow; do not copy its callback, secret switches, or instructions about blocking other apps.

Review least-privilege Account and field access for the bound user. The adapter selects mapped inputs and only writes its six output fields. An administrator session or broad existing profile does not prove restricted permissions. Actual account changes require their own concrete authorization; this guide performs none.

## 3. Review the proposed fields and fictional records

These are proposals from the pinned mapping, not inspected or created Salesforce schema. Check actual describe metadata, exact types, lengths, number precision/scale and picklist values. Missing record values remain unavailable; they are not filled from current or invented data.

| Custom API field | Proposed kind | Purpose |
| --- | --- | --- |
| FR_Industry_Segment__c | Text or reviewed picklist | Exact owned industry label |
| FR_Funding_Stage__c | Text | Funding stage |
| FR_Funding_Observed_On__c | Date | Funding observation date |
| FR_Open_Platform_Roles__c | Number, scale 0 | Exact nonnegative role count |
| FR_Head_Of_Platform_Start__c | Date | Role start date |
| FR_Integration_Pressure__c | Text or reviewed picklist | HIGH, MEDIUM or LOW |
| FR_Integration_Pressure_Basis__c | Text | Preserved nonempty basis |
| FR_Score__c | Number, scale 0 | Original score only |
| FR_Output__c | Text or reviewed picklist | PRIORITIZE or DO_NOT_PRIORITIZE |
| FR_Logic_Version__c | Text | Original logic label |
| FR_Decision_Ref__c | Text | Complete original decision reference |
| FR_Operation_Id__c | Text, at least 64 characters | Exact operation identity |
| FR_Operation_Revision__c | Number, scale 0 | Positive operation revision |

Standard selected fields are Id, Name, Website, LastModifiedDate, NumberOfEmployees and BillingCountry. Only the last six custom fields in the table are write destinations. The full artifact/evaluator identity remains in Flight Recorder. Reuse the existing fictional fixture and shared evaluator for canonical-equivalent, positive-pressure and missing-input records; do not duplicate scores by hand or use real customer data.

## 4. Prepare explicit local files; prove reads first

Existing commands `crm-writer-init --config PATH`, `crm-writer-serve --config PATH`, and `crm-write-status --config PATH` select the local writer-capable format explicitly. Initialization requires new files and never migrates a retained ledger. These commands are not account provisioning, readiness approval, or permission to enable/send. Use the approved qualified interpreter and existing dependencies; never the rejected project virtualenv interpreter.

The following control increment must add and review the readiness enforcement and manual policy/prepare/send/observe commands. They are pending. There is no runnable first-write command in this guide.

After separately authorized setup and sensitive entry, verify the expected identity and selected fields read-only. Confirm the existing n8n runtime/version and route to the loopback adapter before importing or activating anything. The reviewed workflow schedules a 60-second poll and holds only the local ingress reference, never a Salesforce token. Preserve original source values, read interval, artifact and captured Account ETag. Missing or weak ETags remain replay-only.

## 5. Keep preparation, qualification and ordinary sends separate

- `read_only` requests bounded read eligibility from supplied setup declarations. Prior account-read evidence is not required to perform the first separately authorized read.
- `local_prepare` declares local policy selection/preparation only. A prepared operation does not exist until after local policy selection, so this stage cannot require its future operation ID. It makes no provider request and does not authorize a send.
- `qualification_send` names a separately reviewed, fresh, single original-bound test intent. It requires recorded provider identity/read declarations but not proof of the very first qualification write before that probe. `ready_for_write` stays false; eligibility for a controlled test is not ordinary write readiness or atomic-source-guard qualification.
- `ordinary_send` additionally requires separately reviewed actual Account conditional-write proof, including a completed matching write and a separately authorized stale-version 412 case. It is not a retry authority.

For either send stage, later controls must freshly audit the runtime/store and compare the full saved immutable intent with the exact authority: original/capture, artifact/evaluator, mapping/selection, policy generation, target, all six values, and the exact captured source version. The manifest's values are comparison declarations; they never construct or replace the original score. A report or matching declaration hash cannot substitute for this comparison or the owner's separate action authorization.

The six-value digest and source-version digest use canonical JSON SHA-256. The exact source string is preserved; it is never refreshed or substituted into an old intent. An expired declaration cannot release an uncertain target.

Salesforce documents [Account-only If-Match updates](https://developer.salesforce.com/docs/platform/api-rest/guide/resources-sobject-retrieve-patch.html) and [412 without processing when the ETag does not match](https://developer.salesforce.com/docs/platform/api-rest/guide/intro-rest-conditional-requests.html). These documents are not qualification of the selected organization, API version, read fields or actual response framing. Prove those separately with authorized fictional records. Preserve both independently observed versions.

After an authorized single send, observe its complete original-bound output tuple and disable local policy. A lost response remains unknown even if a later read matches. There is no automatic resend, expiry release, forced clear, replacement ETag or buyer-outcome credit. Local fault injection and actual provider HTTP must remain separately labeled. Inspect original and replay in the in-chat browser; current source edits must not change old reconstruction.

Next: finish the gated control build and exact setup review before any real account change or request. No installation, activation, live write, push or deployment is authorized by this guide.
