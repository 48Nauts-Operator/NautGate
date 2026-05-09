"""Findings metadata — categories, severities, descriptions, remediations.

Powers the Privacy / Lighthouse audit view. Categorization mirrors ClawProxy's
working taxonomy (4 categories × 3 severities) so audit reports are directly
comparable across the stack.

Sensitivity (capture-policy gate) and severity (audit weight) are deliberately
separate concepts:
  - sensitivity ∈ {none, pii, secret} → controls body capture (Day 4c policy)
  - severity ∈ {critical, warning, info} → drives Lighthouse score
  - category ∈ {credentials, secrets, pii, infrastructure} → which ring it lands in
"""

from __future__ import annotations

# Rule-id → category. Keys are NautGate's classify.py rule_ids.
CATEGORY: dict[str, str] = {
    # Credentials — API keys + tokens that grant provider access.
    "openai_api_key": "credentials",
    "anthropic_api_key": "credentials",
    "google_api_key": "credentials",
    "aws_access_key_id": "credentials",
    "aws_secret_key": "credentials",
    "github_pat": "credentials",
    "github_personal_token": "credentials",
    "slack_token": "credentials",
    "stripe_key": "credentials",
    "bearer_token": "credentials",
    "generic_api_key": "credentials",
    "azure_connection_string": "credentials",
    "sendgrid_api_key": "credentials",
    "twilio_auth_token": "credentials",
    "http_basic_auth_url": "credentials",
    "database_url": "credentials",
    "nautgate_token": "credentials",
    # Secrets — non-API-key sensitive material.
    "private_key_block": "secrets",
    "jwt": "secrets",
    "generic_secret": "secrets",
    "env_file_content": "secrets",
    # PII — personally identifiable info.
    "email": "pii",
    "phone_us": "pii",
    "ssn_us": "pii",
    "credit_card_like": "pii",
    # Infrastructure — internal-network exposure.
    "ip_address_private": "infrastructure",
    "ssh_key_reference": "infrastructure",
}

# Rule-id → severity. Mirrors ClawProxy's `severity` field.
SEVERITY: dict[str, str] = {
    # Critical — rotate / revoke immediately.
    "openai_api_key": "critical",
    "anthropic_api_key": "critical",
    "google_api_key": "critical",
    "aws_access_key_id": "critical",
    "aws_secret_key": "critical",
    "github_pat": "critical",
    "github_personal_token": "critical",
    "slack_token": "critical",
    "stripe_key": "critical",
    "azure_connection_string": "critical",
    "sendgrid_api_key": "critical",
    "twilio_auth_token": "critical",
    "private_key_block": "critical",
    "ssn_us": "critical",
    "credit_card_like": "critical",
    "database_url": "critical",
    "http_basic_auth_url": "critical",
    "nautgate_token": "critical",
    # Warning — review, may need rotation.
    "bearer_token": "warning",
    "jwt": "warning",
    "generic_secret": "warning",
    "generic_api_key": "warning",
    "env_file_content": "warning",
    "ssh_key_reference": "warning",
    # Info — should be redacted, usually not hostile.
    "email": "info",
    "phone_us": "info",
    "ip_address_private": "info",
}

# User-facing display names for the audit tables.
DISPLAY: dict[str, str] = {
    "openai_api_key": "OpenAI API Key",
    "anthropic_api_key": "Anthropic API Key",
    "google_api_key": "Google API Key",
    "aws_access_key_id": "AWS Access Key",
    "aws_secret_key": "AWS Secret Key",
    "github_pat": "GitHub Token",
    "github_personal_token": "GitHub Personal Token",
    "slack_token": "Slack Token",
    "stripe_key": "Stripe Key",
    "bearer_token": "Bearer Token",
    "generic_api_key": "Generic API Key",
    "azure_connection_string": "Azure Connection String",
    "sendgrid_api_key": "SendGrid API Key",
    "twilio_auth_token": "Twilio Auth Token",
    "http_basic_auth_url": "HTTP Basic Auth URL",
    "database_url": "Database URL",
    "nautgate_token": "NautGate Token",
    "private_key_block": "Private Key Block",
    "jwt": "JWT Token",
    "generic_secret": "Generic Secret",
    "env_file_content": ".env File Content",
    "email": "Email Address",
    "phone_us": "Phone Number",
    "ssn_us": "SSN",
    "credit_card_like": "Credit Card",
    "ip_address_private": "IP Address (Private)",
    "ssh_key_reference": "SSH Key Reference",
}

DESCRIPTION: dict[str, str] = {
    "openai_api_key": "OpenAI API key exposed in message content — could allow unauthorized API usage",
    "anthropic_api_key": "Anthropic API key exposed in message content — could allow unauthorized API usage",
    "google_api_key": "Google API key detected — could enable unauthorized access to Google Cloud services",
    "aws_access_key_id": "AWS access key ID found — paired with a secret key, this grants cloud access",
    "aws_secret_key": "AWS secret access key detected — provides full programmatic access to AWS resources",
    "github_pat": "GitHub access token found — could allow unauthorized repository access",
    "github_personal_token": "GitHub personal access token detected — grants scoped access to GitHub APIs",
    "slack_token": "Slack API token found — could allow reading/sending messages in workspaces",
    "stripe_key": "Stripe API key detected — could allow unauthorized payment operations",
    "bearer_token": "Bearer authentication token found — may grant access to protected APIs",
    "generic_api_key": "API key pattern detected — could grant unauthorized service access",
    "azure_connection_string": "Azure storage connection string — grants access to cloud storage",
    "sendgrid_api_key": "SendGrid API key found — could allow sending emails on your behalf",
    "twilio_auth_token": "Twilio auth token detected — could allow SMS/voice API access",
    "http_basic_auth_url": "URL contains embedded username:password credentials — these can be leaked in logs and browser history",
    "database_url": "Database connection string with credentials — full database access if exposed",
    "nautgate_token": "NautGate API key (ng_…) detected — should not be in prompt content",
    "private_key_block": "Private key (RSA/EC/DSA) detected — this is the most sensitive type of credential",
    "jwt": "JSON Web Token found — may contain encoded user identity and permissions",
    "generic_secret": "Password or secret value detected in content",
    "env_file_content": "Environment variable pattern detected — may contain secrets or config",
    "email": "Personal email address found — PII that may require consent for processing",
    "phone_us": "Phone number detected — PII subject to data protection regulations",
    "ssn_us": "Social Security Number detected — highly sensitive PII requiring strict protection",
    "credit_card_like": "Credit card number detected — PCI DSS regulated data requiring encryption",
    "ip_address_private": "Private/internal IP address found — reveals network topology",
    "ssh_key_reference": "SSH key file reference found — indicates server access credentials",
}

REMEDIATION: dict[str, str] = {
    "openai_api_key": "Rotate this key in your OpenAI dashboard immediately. Use environment variables — never paste keys into prompts.",
    "anthropic_api_key": "Rotate this key in the Anthropic console immediately. Store API keys in environment variables, not conversations.",
    "google_api_key": "Rotate this key in Google Cloud Console. Restrict the key to specific APIs and IP ranges.",
    "aws_access_key_id": "Rotate this key pair in AWS IAM immediately. Use IAM roles or temporary credentials instead of long-lived keys.",
    "aws_secret_key": "Rotate the associated AWS key pair immediately. Never share secret keys — use IAM roles for service access.",
    "github_pat": "Revoke this token in GitHub Settings > Developer settings. Use fine-grained tokens with minimal scope.",
    "github_personal_token": "Revoke and regenerate in GitHub Settings. Use fine-grained PATs scoped to specific repos.",
    "slack_token": "Revoke this token in your Slack app settings. Use bot tokens with minimal OAuth scopes.",
    "stripe_key": "Roll this key in the Stripe Dashboard immediately. Use restricted keys for specific operations.",
    "bearer_token": "Invalidate this token and issue a new one. Avoid pasting auth tokens into LLM conversations.",
    "generic_api_key": "Rotate this key with the service provider. Store keys in environment variables or a vault.",
    "azure_connection_string": "Rotate this connection string in Azure Portal. Use managed identities instead of connection strings.",
    "sendgrid_api_key": "Revoke and regenerate in SendGrid Settings. Use API keys with minimal permissions.",
    "twilio_auth_token": "Rotate your Twilio auth token in the console. Use API keys instead of the master auth token.",
    "http_basic_auth_url": "Remove credentials from URLs. Use environment variables or a secrets manager for authentication.",
    "database_url": "Rotate the database password immediately. Never include connection strings in prompts — use placeholders.",
    "nautgate_token": "Rotate via `just issue-key` and update the client config. Never paste NautGate tokens in prompt content.",
    "private_key_block": "Consider this private key compromised. Generate a new key pair and revoke the old certificate.",
    "jwt": "This JWT may contain identity claims. Invalidate associated sessions and rotate signing keys if needed.",
    "generic_secret": "Change this password or secret immediately. Never paste passwords into LLM conversations.",
    "env_file_content": "Never send .env files to LLMs. Use placeholder values when asking for config help.",
    "email": "Avoid sending email addresses to LLMs unless necessary. Use placeholder values when asking for help with email features.",
    "phone_us": "Avoid including phone numbers in LLM prompts. Use placeholder values for template/formatting questions.",
    "ssn_us": "Never send SSNs to LLMs. If this was real, monitor for identity fraud. Use fake numbers (e.g., 000-00-0000) for examples.",
    "credit_card_like": "Never send card numbers to LLMs. If real, contact your card issuer. Use test card numbers (e.g., 4242...) for examples.",
    "ip_address_private": "Avoid sending internal IPs to LLMs — they reveal network topology. Use RFC 5737 example ranges (192.0.2.x) instead.",
    "ssh_key_reference": "If the referenced key was shared, regenerate it. Never paste SSH key contents into LLM conversations.",
}

# Lighthouse scoring constants. Mirrors ClawProxy's weights so audit reports
# from either tool produce comparable numbers.
CATEGORY_WEIGHTS: dict[str, float] = {
    "credentials": 0.35,
    "secrets": 0.30,
    "pii": 0.25,
    "infrastructure": 0.10,
}
SEVERITY_PENALTY: dict[str, int] = {"critical": 25, "warning": 5, "info": 1}

VERDICTS: list[tuple[int, str, str]] = [
    (90, "Clean", "No significant privacy issues detected in recent traffic."),
    (
        70,
        "Moderate Risk",
        "Minor exposures detected. Review flagged items and consider scrubbing sensitive data before sending.",
    ),
    (
        50,
        "Elevated Risk",
        "Multiple PII or secret patterns detected. Review findings below and rotate any exposed credentials.",
    ),
    (
        0,
        "Critical Exposure",
        "Sensitive credentials or secrets were sent to LLM providers in plain text. These cannot be unsent — rotate all exposed keys and tokens immediately.",
    ),
]


def verdict_for(score: int) -> tuple[str, str]:
    """Return (label, explanation) for an overall Lighthouse score 0..100."""
    for threshold, label, explain in VERDICTS:
        if score >= threshold:
            return label, explain
    return VERDICTS[-1][1], VERDICTS[-1][2]


def category_score(counts: dict[str, int]) -> int:
    """Lighthouse-style 0..100 score for one category given its severity counts."""
    s = 100 - sum(counts.get(sev, 0) * pen for sev, pen in SEVERITY_PENALTY.items())
    return max(0, min(100, s))


def overall_score(cat_scores: dict[str, int]) -> int:
    """Weighted overall 0..100 across all four categories."""
    total = 0.0
    for cat, weight in CATEGORY_WEIGHTS.items():
        total += cat_scores.get(cat, 100) * weight
    return round(total)
