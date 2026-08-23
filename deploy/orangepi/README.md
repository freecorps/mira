# Orange Pi deployment

This deployment runs Mira on an ARM64 Orange Pi while keeping inference in the
existing CLI Proxy container. The proxy and Mira must share the external Docker
network named `shared-network`.

Every successful push to `main` publishes a multi-platform `edge` image to
`ghcr.io/freecorps/mira`. The systemd timer checks that tag every five minutes,
starts the new image, waits for `/health`, and restores the previous image if the
health check fails.

The update contract is exercised in CI on a real `linux/arm64` image. CI
creates a SQLite database with the currently deployed `edge` image, starts the
candidate against it, checks `/health` and the preserved canary row, then
starts the previous image against the same database to prove app rollback. It
also publishes a deliberately unhealthy candidate to an isolated local
registry and invokes `mira-update.sh` end to end, verifying that the updater
restores the healthy image and preserved data.

The host layout is:

```text
/mnt/sda1/mira-stack/
├── compose.yaml
├── data/
└── mira/
    ├── mira.env
    ├── mira.yaml
    └── private-key.pem
```

Install the updater after copying these deployment files to the host:

```bash
sudo install -m 0755 mira-update.sh /usr/local/sbin/mira-update
sudo install -m 0644 mira-update.service /etc/systemd/system/mira-update.service
sudo install -m 0644 mira-update.timer /etc/systemd/system/mira-update.timer
sudo systemctl daemon-reload
sudo systemctl enable --now mira-update.timer
```

Optional updater settings (the defaults match the service above):

| Variable | Default | Purpose |
|---|---|---|
| `MIRA_HEALTH_URL` | `http://127.0.0.1:8000/health` | Endpoint checked after update/rollback |
| `MIRA_HEALTH_ATTEMPTS` | `24` | Maximum health probes per image |
| `MIRA_HEALTH_INTERVAL_SECONDS` | `5` | Delay between probes |
| `MIRA_UPDATE_LOCK_FILE` | `/run/lock/mira-update.lock` | Prevents concurrent updater runs |

Published images include an attached SBOM, max-level BuildKit provenance, and
a GitHub/Sigstore build attestation. Verify the deployed image with:

```bash
gh attestation verify \
  oci://ghcr.io/freecorps/mira:edge \
  --repo freecorps/mira
```

Keep `mira.env`, the GitHub App private key, and CLI Proxy credentials out of
Git. The dashboard and webhook should be published through a TLS reverse proxy;
the container port remains bound to loopback.
