---
title: "Tailscale first: the best way to run NautGate for your whole network"
description: "Put one NautGate server behind Tailscale Serve, keep port 8090 off the public internet, and give every approved device a private HTTPS gateway."
date: 2026-08-17
author: 48Nauts
status: draft
---

# Tailscale first: the best way to run NautGate for your whole network

Running NautGate on a laptop is useful. Running one shared NautGate instance for
every workstation, coding agent, and home-lab machine is where it becomes an
infrastructure layer: one place for provider credentials, routing policy, audit
history, model evidence, and cost.

The wrong way to get there is to publish port `8090` on the internet.

The better pattern is **Tailscale first, NautGate second**:

```text
Claude Code ─┐
Codex ───────┤   encrypted tailnet       localhost only
Pi ──────────┼──► https://nautgate.… ───► 127.0.0.1:8090
your apps ───┘       Tailscale Serve          NautGate
                                                │
                                                ├── NautRouter
                                                └── Postgres
```

NautGate stays on the server's loopback interface. Tailscale Serve provides the
private HTTPS endpoint and makes it reachable only inside your tailnet. There is
no router port-forward, no public firewall rule, and no public reverse proxy.

This guide uses Ubuntu 24.04 and NautGate v0.2.0, but the layout works on any
Linux distribution with Docker Compose v2 and Tailscale.

## What you are building

At the end, you will have:

- one Linux server named `nautgate` in your tailnet;
- NautGate, NautRouter, and Postgres running in Docker;
- NautGate listening only on `127.0.0.1:8090`;
- a private `https://nautgate.<tailnet>.ts.net` URL from Tailscale Serve;
- one NautGate API key per person, device, or agent;
- one shared audit and analytics surface for the whole tailnet.

Every client that should use the gateway joins Tailscale. Devices outside the
tailnet cannot reach it—even if they know the hostname.

## Before you begin

You need:

- an Ubuntu or other Linux server with outbound internet access;
- a Tailscale account and permission to edit the tailnet policy;
- `sudo` access on the server;
- at least one model-provider credential, unless you only want to inspect the
  dashboard first.

You do **not** need a public domain, TLS certificate, inbound firewall rule, or
public IP address.

## 1. Give the server a Tailscale identity

Tailscale recommends tags for non-human machines such as servers. Start in the
**Access controls** page of the Tailscale admin console and define a tag for
NautGate.

The following policy lets every member of the tailnet reach HTTPS on a device
tagged `tag:nautgate`:

```json
{
  "tagOwners": {
    "tag:nautgate": ["autogroup:admin"]
  },
  "grants": [
    {
      "src": ["autogroup:member"],
      "dst": ["tag:nautgate"],
      "ip": ["tcp:443"]
    }
  ]
}
```

If NautGate is for a team rather than everyone in the tailnet, replace
`autogroup:member` with a group such as `group:engineering`.

Tailscale grants are additive. A broad existing allow rule can still grant more
access than this narrow rule, so review the complete policy rather than assuming
the most specific entry wins.

Now install Tailscale on the server:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=nautgate --advertise-tags=tag:nautgate
```

Open the authentication URL printed by `tailscale up`, then confirm the server
appears in the Tailscale **Machines** page with the `tag:nautgate` tag.

For unattended provisioning, use a tagged, pre-approved auth key instead of an
interactive login. Treat that key like a password: inject it from your secret
manager, do not commit it, and unset it after enrollment.

Verify the connection:

```bash
tailscale status
tailscale ip -4
```

Do this before installing NautGate. If the server cannot join the tailnet, you
want to solve that while there is only one moving part.

## 2. Install Docker Compose

On Ubuntu 24.04, the distribution packages are sufficient:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 openssl
sudo systemctl enable --now docker
sudo docker version
sudo docker compose version
```

For another distribution—or if you want Docker's newest engine—follow Docker's
official installation instructions. The important requirement is Compose v2,
invoked as `docker compose`, not the retired `docker-compose` binary.

## 3. Download NautGate and create its secrets

Create a dedicated directory:

```bash
sudo install -d -m 0750 -o "$USER" -g "$(id -gn)" /opt/nautgate
cd /opt/nautgate
curl -fsSL https://nautgate.dev/compose.yml -o docker-compose.yml
```

For a shared service, pin the three NautGate images to the release you tested.
The published Compose file tracks `latest`; this command pins the current guide
to v0.2.0 while leaving `postgres:16` unchanged:

```bash
sed -i 's/:latest/:v0.2.0/g' docker-compose.yml
```

Create a private `.env` with a database password and a stable master key. The
master key protects provider credentials saved through the dashboard. Back it
up: losing it means re-entering those provider keys.

```bash
umask 077
cat > .env <<EOF
POSTGRES_PASSWORD=$(openssl rand -hex 32)
NAUTGATE_MASTER_KEY=$(openssl rand -hex 32)
EOF
chmod 600 .env
```

You can also place `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, or `GEMINI_API_KEY` in this file. We prefer adding provider
keys later through **Settings → Providers**, where NautGate encrypts them at
rest, and keeping `.env` limited to infrastructure secrets.

## 4. Start NautGate—but keep it local

Launch the stack:

```bash
sudo docker compose up -d
sudo docker compose ps
curl -fsS http://127.0.0.1:8090/health
```

The health response should include:

```json
{"status":"ok","version":"0.2.0"}
```

The published Compose file deliberately maps NautGate as
`127.0.0.1:8090:8090`. Leave it that way. Do not replace it with
`0.0.0.0:8090:8090`, do not run `ufw allow 8090`, and do not forward port 8090
on your router.

On first boot, NautGate creates one gateway API key. Capture it now:

```bash
sudo docker compose logs nautgate \
  | grep -oE 'ng_[a-f0-9]{32}_[A-Za-z0-9_-]+' \
  | head -1
```

Store it in your password manager. It is printed only when the database has no
keys yet; recreating the container later does not mint another one.

## 5. Publish localhost privately with Tailscale Serve

Tailscale Serve can terminate HTTPS for the server's MagicDNS name and reverse
proxy requests to NautGate on loopback:

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8090
sudo tailscale serve status
```

If your tailnet has not enabled HTTPS certificates yet, Tailscale will print the
admin action required to enable them. The status output then shows the exact URL,
similar to:

```text
https://nautgate.example-tailnet.ts.net
|-- / proxy http://127.0.0.1:8090
```

Use the URL Tailscale prints; do not copy the example hostname.

This is **Tailscale Serve**, not Tailscale Funnel. Serve stays inside the
tailnet. Funnel deliberately publishes a service to the internet and is the
wrong tool for this deployment.

## 6. Activate the dashboard and add providers

From a second device already connected to the tailnet, open:

```text
https://nautgate.<your-tailnet>.ts.net/dashboard
```

Because this is a remote browser, NautGate intentionally does not inject a local
admin token into the page. Paste the first-run `ng_…` key you saved to activate
the dashboard.

Then:

1. Open **Settings → Providers** and add the providers the network will use.
2. Open **Settings → Keys** and create a separate NautGate key for each user,
   machine, project, or agent.
3. Give clients only their own `ng_…` key—not the provider credential and not
   the first-run admin key.

Separate keys make the audit log useful. A single shared token can tell you that
the network called a model; per-client keys tell you who or what made the call.
They also let you revoke one compromised client without interrupting everyone.

## 7. Point the network at NautGate

Copy the HTTPS base URL from `tailscale serve status`.

For OpenAI-compatible clients:

```bash
export OPENAI_BASE_URL=https://nautgate.<your-tailnet>.ts.net/v1
export OPENAI_API_KEY=ng_your_client_key
```

For Claude Code:

```bash
export ANTHROPIC_BASE_URL=https://nautgate.<your-tailnet>.ts.net
export ANTHROPIC_API_KEY=ng_your_client_key
claude --bare
```

`--bare` matters when Claude Code already has a stored OAuth login; otherwise
that login can take precedence over the endpoint and key you just supplied.

Use the same pattern for applications and scripts: the Tailscale Serve URL is
the base URL, and a client-specific `ng_…` token is the API key. Provider keys
remain on the server.

## 8. Prove it from another device

From a tailnet client—not from the NautGate server—run:

```bash
curl -fsS https://nautgate.<your-tailnet>.ts.net/health
```

Then make one real model call from a configured client and confirm that the
dashboard records:

- the client or agent identity;
- the requested and actually served model;
- the provider response, latency, tokens, and cost;
- the route decision that led there.

Finally, disconnect that client from Tailscale and retry the health URL. It
should no longer be reachable. That negative test is the proof that NautGate is
private, not merely obscure.

## Operating the shared gateway

### Check it

```bash
cd /opt/nautgate
sudo docker compose ps
sudo docker compose logs --tail=100 nautgate
sudo tailscale serve status
```

From another tailnet device:

```bash
tailscale ping nautgate
curl -fsS https://nautgate.<your-tailnet>.ts.net/ready
```

`/health` proves the process is alive. `/ready` also checks whether the database
is reachable.

### Update it deliberately

For a shared gateway, read the release notes, change the three NautGate image
tags in `docker-compose.yml`, and then run:

```bash
cd /opt/nautgate
sudo docker compose pull
sudo docker compose up -d
curl -fsS http://127.0.0.1:8090/health
```

The Postgres data and NautGate backups live in Docker volumes and survive a
normal recreate. Do not use `docker compose down -v` unless you intend to delete
them.

### Stop sharing without stopping NautGate

```bash
sudo tailscale serve off
```

That removes the tailnet-facing proxy while leaving the loopback service
running. To stop the application too:

```bash
cd /opt/nautgate
sudo docker compose down
```

## What about devices that cannot run Tailscale?

The cleanest design is one Tailscale client per workstation or server. It gives
each device an identity and lets grants decide who reaches NautGate.

A Tailscale subnet router can bring legacy LAN devices into the picture, but it
also widens the trust boundary: access becomes associated with a routed network
rather than an individually authenticated device. Add one only when you have a
real client that cannot run Tailscale, and give that subnet the narrowest grant
possible. It is not required for the setup in this article.

## The security checklist

Before calling the deployment finished, verify all of these:

- [ ] Tailscale was installed and tested before NautGate.
- [ ] The server has a non-human `tag:nautgate` identity.
- [ ] Tailnet grants allow only the intended users to reach TCP 443.
- [ ] Broad allow rules do not accidentally bypass the narrow grant.
- [ ] Docker publishes NautGate only on `127.0.0.1:8090`.
- [ ] No public firewall or router rule exposes port 8090.
- [ ] Tailscale Serve—not Funnel—provides the HTTPS endpoint.
- [ ] `.env` is mode 0600 and its master key is backed up securely.
- [ ] Every user, device, or agent has a separate NautGate key.
- [ ] Provider credentials stay on the server.
- [ ] A second tailnet device can connect, and a non-tailnet device cannot.

That is the whole pattern: **identity and private networking first; gateway
second**. NautGate can then serve an entire team without becoming another public
admin panel waiting to be found.

## Further reading

- [Install Tailscale on Linux](https://tailscale.com/docs/install/linux)
- [Set up servers with tags and auth keys](https://tailscale.com/docs/servers)
- [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve)
- [Tailscale Serve command reference](https://tailscale.com/docs/reference/tailscale-cli/serve)
- [Tailscale grants](https://tailscale.com/docs/features/access-control/grants)
- [MagicDNS](https://tailscale.com/docs/features/magicdns)
- [NautGate v0.2.0](https://github.com/48Nauts-Operator/NautGate/releases/tag/v0.2.0)
