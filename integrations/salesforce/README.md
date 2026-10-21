# Inactive Salesforce polling template

This export has two nodes: a one-minute schedule and an authenticated POST to the local Flight Recorder adapter. The adapter is the sole owner for CRM authentication and HTTP operations. This template requests polling only. The workflow contains no CRM credential, write node or inline bearer secret.

The export is inactive. No n8n installation, import, connection or activation has been performed. Its connection and credential references are explicit setup placeholders. Before setup, review the actual n8n version, import schema, local network and credential handling alongside the Salesforce readiness manifest. A changed configured export needs its own exact review; `validate_n8n_export` accepts only this frozen template. It does not import or activate anything.

`localhost:8768` requires n8n to run in the same host/network context as the adapter. A container or remote n8n cannot use this address unchanged. A different network arrangement requires a concrete later decision; do not expose the collector or local controls to make it work.

The body includes only the reviewed connection ID and a workflow/execution ID pair. HTTP-node retries within that execution preserve the same tick. A new schedule execution has a new tick. A 202 response means durable receipt, not a Salesforce read, decision or write. Excess ticks can be durably coalesced. Polling observes selected reads and can miss changes between them.

The node parameters were checked against current [Schedule Trigger documentation](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.scheduletrigger), [HTTP Request documentation](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest), and the official [HTTP node parameter source](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/HttpRequest/V3/Description.ts). That inspection does not prove compatibility with an unselected runtime.
