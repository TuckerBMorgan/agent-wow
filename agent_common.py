"""Shared harness settings and credential redaction for host and container tools."""
import json
import os
import re


HARNESSES = {
    'codex': {'model_env': 'CODEX_MODEL', 'effort_env': 'CODEX_REASONING_EFFORT',
              'default_model': 'gpt-6.1-sol', 'auth_env': 'CODEX_AUTH_JSON'},
    'claude': {'model_env': 'CLAUDE_MODEL', 'effort_env': 'CLAUDE_EFFORT',
               'default_model': 'claude-opus-5-5', 'auth_env': 'CLAUDE_CODE_OAUTH_TOKEN'},
}


def harness_spec(harness):
    if harness not in HARNESSES:
        raise ValueError('Harness must be codex or claude.')
    return HARNESSES[harness]


def model_settings(harness):
    spec = harness_spec(harness)
    model = os.environ.get(spec['model_env'], spec['default_model'])
    effort = os.environ.get(spec['effort_env'], 'medium')
    prefix = spec['default_model'].split('-')[0]
    if not re.fullmatch(prefix + r'-[A-Za-z0-9.-]+', model):
        raise ValueError(f'{spec["model_env"]} must be a native model ID for {harness}.')
    if effort not in ('low', 'medium', 'high', 'xhigh', 'max'):
        raise ValueError(f'{spec["effort_env"]} must be low, medium, high, xhigh, or max.')
    return model, effort


def credential_redactor():
    # Capture before a runner removes credentials from its child environment.
    values = {os.environ[name] for name in (
        'CODEX_API_KEY', 'OPENAI_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN',
        'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CODEX_AUTH_JSON',
    ) if os.environ.get(name)}
    try:
        tokens = json.loads(os.environ.get('CODEX_AUTH_JSON') or '{}').get('tokens', {})
        values.update(value for value in tokens.values() if isinstance(value, str) and value)
    except (ValueError, AttributeError, TypeError):
        pass  # Login validation belongs to the selected harness.
    secrets = sorted(values, key=len, reverse=True)

    def redact(data):
        for secret in secrets:
            if isinstance(data, bytes):
                data = data.replace(secret.encode(), b'[REDACTED]')
            else:
                data = data.replace(secret, '[REDACTED]')
        return data

    return redact
