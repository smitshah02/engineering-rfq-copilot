# Source from the repository root: source scripts/dev_env.sh
# Defines the development launcher; sourcing this file starts no services.
rfq_dev() {
  .venv/bin/python - "$@" <<'PY'
import os
import sys

from dotenv import dotenv_values

urls = {
    'api': 'APP_DATABASE_URL',
    'decision': 'DECISION_DATABASE_URL',
    'mcp': 'APP_DATABASE_URL',
    'worker': 'WORKFLOW_DATABASE_URL',
    'drafter': 'DRAFTER_DATABASE_URL',
}
prefixes = {
    'api': ('RFQ_API_',),
    'decision': ('RFQ_API_',),
    'mcp': ('RFQ_MCP_',),
    'worker': ('RFQ_WORKFLOW_', 'RFQ_MCP_'),
    'drafter': ('RFQ_DRAFTER_',),
}
if len(sys.argv) < 3 or sys.argv[1] not in urls:
    raise SystemExit('Usage: rfq_dev {api|decision|mcp|worker|drafter} COMMAND [ARGS...]')
role, *command = sys.argv[1:]
source = dict(dotenv_values('.env', interpolate=False)) | dict(os.environ)
keys = {'DATABASE_HOST', 'DATABASE_PORT', 'DEVELOPMENT_DATABASE_NAME', urls[role]}
if role in {'mcp', 'worker'}:
    keys.add('RFQ_API_JWT_AUDIENCE')
env = {
    k: v for k, v in source.items()
    if v is not None and (k in keys or k.startswith(prefixes[role]))
}
for k in ('RFQ_API_TARGET', 'RFQ_DECISION_TARGET'):
    env.pop(k, None)
env.update({k: os.environ[k] for k in ('PATH', 'HOME', 'TMPDIR', 'LANG') if k in os.environ})
if role in {'api', 'decision'}:
    env['RFQ_API_ENVIRONMENT'] = 'development'
if role == 'api':
    env['RFQ_API_WORKFLOW_QUEUE'] = 'enabled'
    env['RFQ_API_PERMISSION_POLICY'] = 'v3'
if role == 'worker':
    env['RFQ_WORKFLOW_ENVIRONMENT'] = 'development'
if role == 'mcp':
    env['RFQ_MCP_DATABASE_TARGET'] = 'development'
os.execvpe(command[0], command, env)
PY
}
