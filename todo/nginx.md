# nginx in front of the provider — recommendation (not needed for internal testing)

**Status: proposal, deliberately unbuilt.** The Perl provider sat behind an nginx reverse
proxy; this records whether to repeat that here, and why the answer is "not yet". Like
`todo/index.md`, this is a decision to stop re-deriving — it is not a task.

## Verdict for the current use

**Skip it while the box is internal testing.** The app serves the six verbs correctly on
`:8000`, the LAN is trusted, and this is the recreation, not the deployment — the colleague's
server is where a proxy belongs. nginx here would add a moving part without changing any test
outcome.

## When it earns its place

- The test is meant to **mirror the colleague's deployment** rather than prove the provider.
- The service goes anywhere semi-public, or an aggregator requires `https`.

## What it buys

- **TLS** — many harvesters, and Europeana-style aggregators, expect `https`. Self-signed for
  internal, Let's Encrypt for a real public name.
- **One public port** — nginx on 443, app on `127.0.0.1:8000`, and BaseX's full read/write
  XQuery endpoint on `:8080` stops being reachable at all. This is the defense-in-depth worth
  having once "trusted LAN" is a weaker assumption than it is at home.
- **gzip** — `ListRecords` with `ria`/`lido` payloads is large; this is the concrete bandwidth win.
- A place for rate limits, access logs, and a stable `/oai` path.

## Why this provider is unusually proxy-friendly

The classic OAI reverse-proxy trap is `baseURL` being derived from `Host` /
`X-Forwarded-Proto` and ending up wrong (http vs https, internal name vs public). Here
`baseURL` comes from **`OAI_BASE_URL` by explicit config** — `Identify` echoes it and it is
never inferred from headers. So a proxy is just: set `OAI_BASE_URL` to the public URL, and
`proxy_pass` straight through. No header plumbing, and resumption tokens / `POST` bodies pass
through untouched by a plain `proxy_pass`.

## Practical blocker on this machine

nginx is **not installed** (no `nginx`/`caddy`/`traefik`), `/etc/nginx` does not exist, and
there is **no passwordless sudo**. A system nginx listening on 80/443 is therefore a root step —
one the user runs, never one to take a password for.

## Two shapes

1. **Reference config now, run it on the real box.** `deploy/nginx.conf` (below) committed and
   documented, installed by the user on the colleague's server. Changes nothing here.
2. **Run it here, rootless.** A user-space nginx on a high port (e.g. `:8088`) as a
   `systemd --user` service, like BaseX. No sudo, but it cannot be 80/443, so the URL is
   `http://host:8088/oai` — fine for a functional test, not representative of the real front door.

## Draft server block (NOT yet validated against a live nginx)

```nginx
server {
    listen 443 ssl;
    server_name oai.example.org;

    ssl_certificate     /etc/letsencrypt/live/oai.example.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/oai.example.org/privkey.pem;

    # The real win: ListRecords with ria/lido payloads is large.
    gzip on;
    gzip_types text/xml application/xml;

    location = /healthz {
        proxy_pass http://127.0.0.1:8000/healthz;
    }

    # No trailing path on proxy_pass: the URI (/oai) is preserved. POST bodies
    # and resumption tokens pass through a plain proxy_pass untouched. Host /
    # X-Forwarded-* need no special handling because baseURL is explicit config.
    location /oai {
        proxy_pass http://127.0.0.1:8000;
    }

    # :8080 (BaseX) is never proxied; it stays internal.
}
```

With this in place `OAI_BASE_URL` must be the public URL (`https://oai.example.org/oai`), and
the app must be restarted after changing it — a running process keeps the old value (the same
trap noted in `AGENTS.md`). Because that URL is non-loopback, the secrets guard in
`oai/config.py` also requires real `OAI_BASEX_PASSWORD` / `OAI_TOKEN_SECRET`.

## Open questions

- Does the colleague's deployment already terminate TLS somewhere upstream (an institutional
  proxy or load balancer)? If so, a local nginx only adds a hop.
- Does the target aggregator require `https`, or is plain `http://host/oai` accepted?
- Is the provider mounted at the domain root (`/oai`) or under a subpath? The app's routes are
  root-based, so a subpath means an nginx rewrite — worth settling before the public `baseURL`
  is published, since changing it later re-addresses every harvested record.
