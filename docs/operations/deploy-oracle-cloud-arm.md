# Staging on an Oracle Cloud Always Free Arm VM

**Audience:** the project owner, setting up the first staging host (#198) and the parts of TASK-078, TASK-079,
TASK-102 and TASK-103 that need one. **Scope:** one Ampere A1 (Arm64) VM running
`infra/deploy/docker-compose.prod.yml` exactly as `docs/operations/runbook.md` describes it. Day-2 operations
(rollback, secret rotation, restore drills, incidents) stay in the runbook; this guide only gets the host to the
point where the runbook applies.

Conventions: `<placeholder>` values are yours. `laptop$` commands run on your machine from a checkout of the
repository, `vm$` commands run on the VM. Never paste secrets, tokens or customer data into issues, chat or this
document.

**What was checked, and what was not.** Every repository path, script, flag and `make` target below was checked
against the repository on 2026-11-04. The shell scripts pass `bash -n`; the production compose file was resolved with
`docker compose config` using `.env.prod.example` with dummy values; the systemd units in section 17 pass
`systemd-analyze verify`; the private-CA `openssl` commands in section 9 were run. **None of it has run on Oracle
Cloud yet**: the console steps follow Oracle's documented flow, but menu names and free-tier limits change. Where a
limit matters, the guide says "check the current limit" instead of quoting a number as fact.

---

## 1. Decide three things first

| Decision | Option A (recommended) | Option B | Consequence |
|---|---|---|---|
| Host name and TLS | A DNS name (your domain, or a free dynamic-DNS subdomain) with a Let's Encrypt certificate (section 9.1) | The bare IP address with a private CA you create (section 9.2) | The GitHub deploy job's smoke test only trusts public CAs, so **option B means manual deploys only** |
| SSH (port 22) | Open only to your IP; deploy by hand from your laptop (section 12) | Also open to GitHub-hosted runners, so the `deploy` job of `deploy.yml` can reach the host (section 13) | GitHub-hosted runners have no fixed address. Option B means port 22 open to the internet (key-only), at least for the length of a release run |
| Data on staging | Empty: customers register themselves | Demo data | **Demo data is not possible on staging by design**: `scripts/seed_dev.py` and `scripts/seed_load.py` refuse `APP_ENV=staging` (section 14) |

Ports: 443 (the site), 22 (SSH, restricted), and 80 only with option A for TLS (Let's Encrypt HTTP-01 challenges and
the HTTP to HTTPS redirect; nginx serves nothing else on it). Nothing else is ever opened: PostgreSQL, Redis, the API
port and `/metrics` are not published by the production compose file.

## 2. Oracle Cloud account and region

1. Sign up at the Oracle Cloud Free Tier page. A card is asked for identity verification.
2. **Home region:** choose it carefully, it cannot be changed later, and Always Free Ampere A1 capacity is only
   available in the home region. From India, `India West (Mumbai)` (`ap-mumbai-1`) or `India South (Hyderabad)`
   (`ap-hyderabad-1`) keep the latency of the page-load measurements (TASK-102, section 18.1) low.
3. Check the current Always Free limits on Oracle's free-tier page before sizing anything. At the time of writing they
   included Ampere A1 compute up to a total of 4 OCPUs and 24 GB of memory, a block-storage allowance shared with boot
   volumes, and a small Object Storage allowance (used for backups in section 15). Treat these as "check current
   free-tier limits", not as guarantees.
4. Set a budget with an alert (Billing & Cost Management > Budgets) at a small amount, so any resource that is
   accidentally outside the free tier is noticed in days, not at the end of the month.
5. Oracle documents that idle Always Free compute instances can be reclaimed. A staging host that sits idle for a
   week may qualify. Read the current "idle Always Free compute" rule on Oracle's free-tier FAQ. Upgrading the
   account to Pay As You Go removes that rule while Always Free resources stay free, but anything beyond the
   free limits is then billed, so do it only with the budget alert from step 4 in place.

## 3. Network: VCN and security list

Networking > Virtual cloud networks > **Start VCN Wizard** > "Create VCN with Internet Connectivity". Accept the
defaults (a public subnet with an internet gateway). Then edit the public subnet's **Default Security List**:

| Ingress rule | Source CIDR | Protocol / port | Why |
|---|---|---|---|
| Edit the existing SSH rule | `<your-ip>/32` (find it with `laptop$ curl -s https://checkip.amazonaws.com`) | TCP 22 | Administration. The wizard's default is `0.0.0.0/0`: replace it |
| Add | `0.0.0.0/0` | TCP 443 | The site. Use `<your-ip>/32` instead if staging should only be reachable by you |
| Add (option A of section 1 only) | `0.0.0.0/0` | TCP 80 | Let's Encrypt HTTP-01 and the redirect to HTTPS |

Leave the default ICMP rules (path MTU discovery) and the egress rule (the host pulls images and packages).

If your home IP changes, SSH stops working until you update the 22 rule in the console.

## 4. SSH keys

Two separate key pairs, so the GitHub secret can be revoked without locking you out:

```bash
laptop$ ssh-keygen -t ed25519 -f ~/.ssh/pbip-oci-admin -C "pbip staging admin"
laptop$ ssh-keygen -t ed25519 -f ~/.ssh/pbip-deploy -C "pbip staging deploy (GitHub Actions)"
```

Give each a passphrase except `pbip-deploy`, which GitHub Actions must use unattended (it becomes the
`DEPLOY_SSH_KEY` secret in section 13 and never leaves GitHub and your laptop).

## 5. Create the VM

Compute > Instances > **Create instance**:

* **Image:** Canonical Ubuntu 24.04 (not "Minimal"). With an Ampere shape the console selects the aarch64 build.
  Ubuntu 24.04 ships Python 3.12, which `scripts/uptime_report.py` and `scripts/staging_smoke.py` need (standard
  library only).
* **Shape:** Ampere > `VM.Standard.A1.Flex`, **4 OCPUs and 24 GB** if that is still the free maximum (check current
  free-tier limits). The stack needs far less (two API replicas use about one core each under load, ADR-0045), but
  the performance runs in section 18 benefit from the headroom.
* **Networking:** the VCN and public subnet from section 3, "Assign a public IPv4 address".
* **SSH keys:** paste `~/.ssh/pbip-oci-admin.pub`.
* **Boot volume:** the default size is enough; keep the total within the free block-storage allowance.

"Out of capacity" for A1 is common in busy regions. Try another availability domain in the same region, or retry
later; it is a capacity message, not an account problem.

Note the public IP. If the VM may ever be recreated, consider converting the address to a **reserved** public IP
(Networking > IP management) so DNS does not change; check that it stays within your free allowance.

## 6. First login, deploy user, SSH hardening

```bash
laptop$ ssh -i ~/.ssh/pbip-oci-admin ubuntu@<vm-ip>
vm$ sudo apt-get update && sudo apt-get -y full-upgrade && sudo reboot
```

Create the account that owns `~/pbip` (the runbook's "deployment host" user) and give it both public keys:

```bash
vm$ sudo adduser --disabled-password --gecos "" pbip
vm$ sudo install -d -m 700 -o pbip -g pbip /home/pbip/.ssh
laptop$ cat ~/.ssh/pbip-oci-admin.pub ~/.ssh/pbip-deploy.pub | \
          ssh -i ~/.ssh/pbip-oci-admin ubuntu@<vm-ip> 'sudo tee /home/pbip/.ssh/authorized_keys >/dev/null'
vm$ sudo chown pbip:pbip /home/pbip/.ssh/authorized_keys && sudo chmod 600 /home/pbip/.ssh/authorized_keys
laptop$ ssh -i ~/.ssh/pbip-oci-admin pbip@<vm-ip> 'id'      # must work before you continue
```

Confirm key-only SSH (Oracle's Ubuntu images ship this way; check, do not assume):

```bash
vm$ sudo sshd -T | grep -E '^(passwordauthentication|permitrootlogin|kbdinteractiveauthentication) '
# expected: passwordauthentication no, permitrootlogin without-password (or no), kbdinteractiveauthentication no
```

Ubuntu installs `unattended-upgrades` with security updates enabled; check with
`vm$ systemctl status unattended-upgrades`. The VM's clock is UTC by default, which the probe log and the Celery beat
checks (section 19) assume.

## 7. Host firewall on Oracle's Ubuntu image

Oracle's Ubuntu images ship their own `iptables` rules (`/etc/iptables/rules.v4`, loaded by `netfilter-persistent`):
the `INPUT` chain accepts SSH and rejects everything else, and the `FORWARD` chain rejects by default. Oracle's
documentation advises against enabling `ufw` on these images; do not enable it. (`ufw` would not filter Docker's
published ports anyway, because Docker forwards them before `ufw`'s rules are reached.)

Docker publishes ports through NAT and the `FORWARD` chain, and inserts its own `ACCEPT` rules at the top of
`FORWARD` when it starts, above Oracle's `REJECT`. So no host rule is needed for 80/443: the **security list
(section 3) is the perimeter**. Two rules follow from this:

* Never run `netfilter-persistent save` or `iptables-restore` while Docker is running: the first saves Docker's
  dynamic rules into the boot file, the second flushes them and cuts every container off. If it happened, run
  `vm$ sudo systemctl restart docker` (Docker re-creates its rules).
* If the site is unreachable after the first deploy, check the order: in
  `vm$ sudo iptables -L FORWARD -n --line-numbers` the `DOCKER-USER` / `DOCKER-FORWARD` jumps must come before the
  `REJECT` line.

## 8. Docker Engine and the Compose plugin

Docker's official apt repository (arm64 is supported):

```bash
vm$ sudo apt-get update
vm$ sudo apt-get install -y ca-certificates curl
vm$ sudo install -m 0755 -d /etc/apt/keyrings
vm$ sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
vm$ sudo chmod a+r /etc/apt/keyrings/docker.asc
vm$ echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
vm$ sudo apt-get update
vm$ sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
vm$ sudo usermod -aG docker pbip
vm$ sudo -iu pbip docker run --rm hello-world          # prints "(arm64v8)" in its text
vm$ sudo -iu pbip docker compose version              # deploy.sh needs --wait-timeout (Compose v2.17+)
```

Membership of the `docker` group is equivalent to root on this host. That is accepted here because `pbip` is the only
login that operates the stack; never add other users to the group.

## 9. Host name and TLS

nginx (`web`) terminates TLS with `~/pbip/certs/web/tls.{crt,key}`. `infra/deploy/deploy.sh` creates a 30-day
placeholder there if none exists and **never overwrites** an existing pair. Pick one path.

### 9.1 Option A: a DNS name and Let's Encrypt (needed for GitHub deploys)

1. **Name.** Either an `A` record for a domain you own (for example `staging.<your-domain>` to `<vm-ip>`), or a free
   dynamic-DNS subdomain (DuckDNS and similar services) pointed at `<vm-ip>`. Let's Encrypt rate limits are counted
   per registered domain. A free subdomain is only reliable if its parent domain is on the Public Suffix List
   (https://publicsuffix.org); otherwise everyone on that service shares one limit. Wildcard-IP names
   (`<ip>.sslip.io`, `nip.io`) work for DNS but hit those shared limits quickly.
2. Check from your laptop: `laptop$ getent hosts <host>` (or `dig +short <host>`) returns `<vm-ip>`.
3. Set `PUBLIC_HOSTNAME=<host>` and `CORS_ORIGINS=https://<host>` in the host `.env` (section 11), deploy
   (section 12), then issue the certificate with the runbook's command (runbook section 1.4, step 2):

   ```bash
   vm$ sudo apt-get install -y certbot
   vm$ sudo certbot certonly --webroot -w /home/pbip/pbip/acme -d <host> --agree-tos -m <ops-email> \
         --deploy-hook "PBIP_DIR=/home/pbip/pbip bash /home/pbip/pbip/certbot-deploy-hook.sh"
   vm$ sudo certbot renew --dry-run
   ```

   Port 80 must stay open for renewals (the packaged `certbot.timer` renews automatically).

### 9.2 Option B: the IP address and a private CA (manual deploys only)

Browsers and the GitHub runner will not trust this certificate, so the deploy job's smoke test fails and rolls back:
use this path only with manual deploys (section 12). On the laptop, create a small CA and a certificate for the IP
(keep `ca.key` offline; it can issue certificates your laptop trusts):

```bash
laptop$ mkdir -p ~/pbip-staging-ca && cd ~/pbip-staging-ca && IP=<vm-ip>
laptop$ openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 825 \
          -subj "/CN=PBIP staging CA" -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
          -addext "keyUsage=critical,keyCertSign,cRLSign" -keyout ca.key -out ca.crt
laptop$ openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=$IP" \
          -keyout tls.key -out tls.csr
laptop$ printf 'subjectAltName=IP:%s\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n' "$IP" > leaf.ext
laptop$ openssl x509 -req -in tls.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 397 -sha256 \
          -extfile leaf.ext -out tls.crt
laptop$ openssl verify -CAfile ca.crt tls.crt                         # tls.crt: OK
laptop$ ssh -i ~/.ssh/pbip-oci-admin pbip@<vm-ip> 'mkdir -p ~/pbip/certs/web && chmod 700 ~/pbip/certs && chmod 755 ~/pbip/certs/web'
laptop$ scp -i ~/.ssh/pbip-oci-admin tls.crt tls.key pbip@<vm-ip>:pbip/certs/web/
laptop$ scp -i ~/.ssh/pbip-oci-admin ca.crt pbip@<vm-ip>:pbip/certs/staging-ca.crt   # public; the probe trusts it
laptop$ ssh -i ~/.ssh/pbip-oci-admin pbip@<vm-ip> 'chmod 644 ~/pbip/certs/web/tls.crt ~/pbip/certs/web/tls.key'
```

`tls.key` is mode 0644 inside the 0700 `certs` directory for the same reason as in the runbook: nginx runs as uid 101
and reads it through a bind mount. Set `PUBLIC_HOSTNAME=<vm-ip>` and `CORS_ORIGINS=https://<vm-ip>`. Command-line
clients trust the CA with `curl --cacert ~/pbip-staging-ca/ca.crt`, `SSL_CERT_FILE=~/pbip-staging-ca/ca.crt`
(Python) or `--cacert` (`scripts/staging_smoke.py`). Do not import it into your everyday browser profile; use a
separate profile if you need the UI. Browsers ignore HSTS for IP addresses, so the site's two-year HSTS header does
not pin anything here. Re-issue `tls.crt` before 397 days, the same way.

## 10. Images for arm64, and pulling them from GHCR

`deploy.yml` (`package` job) builds `ghcr.io/rachitst/pbip/api` and `.../web` when a `vX.Y.Z` tag is pushed. Until
GitHub PR #432 ("ci(images): build linux/amd64 and linux/arm64 images for the Arm staging host") is
merged, it built **linux/amd64 only**, and an Arm VM fails with
`no matching manifest for linux/arm64/v8`. After it is merged, tag a new release; check before deploying (both
lines must appear; the packages are private, so run the GHCR login below on the laptop too):

```bash
laptop$ docker buildx imagetools inspect ghcr.io/rachitst/pbip/api:<vX.Y.Z> | grep -E 'Platform: +linux/(amd64|arm64)'
laptop$ docker buildx imagetools inspect ghcr.io/rachitst/pbip/web:<vX.Y.Z> | grep -E 'Platform: +linux/(amd64|arm64)'
```

**GHCR access.** New GHCR packages are private, whatever the repository's visibility, so the host must log in once
(`docs/operations/private-repo-checklist.md` section 4.5). Create a **classic** personal access token with only the
`read:packages` scope, then on the VM as `pbip`:

```bash
vm$ read -rs GHCR_TOKEN && printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u <github-user> --password-stdin; unset GHCR_TOKEN
```

Docker stores it in `~/.docker/config.json` (base64, not encrypted; it warns about this). The token can only read
packages; keep the file at mode 600 and revoke the token if the host is ever rebuilt or compromised.

## 11. Files and secrets on the host

Copy the same files the deploy job copies, plus the probe script (section 17):

```bash
laptop$ ssh -i ~/.ssh/pbip-oci-admin pbip@<vm-ip> 'mkdir -p ~/pbip'
laptop$ scp -i ~/.ssh/pbip-oci-admin infra/deploy/docker-compose.prod.yml infra/deploy/deploy.sh \
          infra/deploy/certbot-deploy-hook.sh infra/postgres/tls-entrypoint.sh infra/deploy/uptime-probe.sh \
          scripts/uptime_report.py infra/deploy/.env.prod.example pbip@<vm-ip>:pbip/
```

Create the host `.env` from the template **on the VM** (the secrets are generated there and never leave it except into
your password manager):

```bash
vm$ cd ~/pbip && install -m 600 .env.prod.example .env
vm$ python3 -c "import secrets; print(secrets.token_urlsafe(18))"          # POSTGRES_PASSWORD
vm$ python3 -c "import secrets; print(secrets.token_urlsafe(48))"          # JWT_SECRET
vm$ python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"   # FIELD_ENCRYPTION_KEY
vm$ nano .env
```

The last line is exactly what `Fernet.generate_key()` (the template's command) does, without needing the
`cryptography` package on the host. Fill in every `<placeholder>`: the three secrets above, `CORS_ORIGINS=https://<host>`, `PUBLIC_HOSTNAME=<host>`,
`IMAGE_PREFIX=ghcr.io/rachitst/pbip`, `IMAGE_TAG=<vX.Y.Z>`. Keep `APP_ENV=staging` and `COOKIE_SECURE=true`: with
those, the API refuses to boot with weak or development secrets (runbook section 1.2). Then:

```bash
vm$ chmod 600 .env && rm .env.prod.example
vm$ docker compose -f docker-compose.prod.yml config --quiet && echo "compose file and .env resolve"
```

Store the three secret values in your password manager under "PBIP staging". `.env` is never committed and never
copied back to the laptop. Losing `FIELD_ENCRYPTION_KEY` makes the encrypted columns of every backup unreadable
(runbook sections 3.2 and 4.1).

## 12. First deployment by hand, and migrations

```bash
vm$ cd ~/pbip && bash deploy.sh deploy ghcr.io/rachitst/pbip <vX.Y.Z>
vm$ docker compose -f docker-compose.prod.yml ps                  # migrate exited 0, the rest healthy
vm$ docker compose -f docker-compose.prod.yml logs migrate | tail -20  # alembic upgrades to head
vm$ cat .deployed_tag
```

What happens (runbook section 2.1): `deploy.sh` creates `certs/db` (PostgreSQL TLS) and, if absent, the placeholder web
certificate; pulls the images; starts `db` and `redis`; runs `migrate` (`alembic upgrade head`) to completion before any
`api` replica starts; starts `web` once `api` is healthy; and records the tag only when every container is healthy
within 180 s. Migrations run on every deploy and are a no-op when the schema is current; they are never downgraded
automatically (runbook section 2.2). With ADR-0045 merged, `api` runs as `API_REPLICAS` (default 2) replicas.

Option A: now run certbot (section 9.1, step 3). Then, from the laptop:

```bash
laptop$ curl -sI http://<host>/ | grep -iE '^(HTTP|location)'          # option A: 301 to https
laptop$ python3 scripts/smoke_test.py https://<host>                    # option B: prefix SSL_CERT_FILE=~/pbip-staging-ca/ca.crt
laptop$ export PBIP_SMOKE_EMAIL=<smoke-customer-email>; read -rs PBIP_SMOKE_PASSWORD && export PBIP_SMOKE_PASSWORD
laptop$ python3 scripts/staging_smoke.py https://<host> --check-http-redirect   # option B: --cacert ~/pbip-staging-ca/ca.crt, no redirect flag
```

`scripts/staging_smoke.py` verifies the TLS chain, host name, protocol (TLS 1.2+) and certificate lifetime, HSTS,
liveness and readiness, then signs in as a customer, lists that customer's accounts (read-only) and signs out. Create
the smoke-test customer once through the web app's registration page (an account without an authenticator) and keep
its password in your password manager. Credentials are read from the environment only and never printed; `read -rs`
keeps the password out of your shell history.

## 13. Deploying through GitHub Actions (#198)

The `deploy` job connects with SSH from a GitHub-hosted runner. Those runners have no fixed address, so with port 22
restricted to your IP (section 3) the job cannot connect. Choose, per release:

* **Manual (default):** leave `DEPLOY_ENABLED` unset. A tag push still verifies the tag and publishes both images;
  you deploy with section 12. This proves `deploy.sh`, not the workflow's SSH steps. (With `DEPLOY_ENABLED=true` and
  port 22 closed, every tag push ends in a failed `deploy` job, and the dry run also needs `DEPLOY_ENABLED=true`.)
* **Through the workflow:** for the duration of the run, change the security-list rule for TCP 22 to `0.0.0.0/0`,
  then set it back to `<your-ip>/32`. The job authenticates with the dedicated deploy key, verifies the host key
  (`StrictHostKeyChecking yes`) and uses batch mode; password login is off (section 6). For a permanently open port 22,
  also install `fail2ban` (`vm$ sudo apt-get install -y fail2ban`; Ubuntu's package enables the `sshd` jail).

One-time GitHub set-up (names only are checked by the workflow; values never appear in logs). Build the
`known_hosts` line from the key the VM itself reports, over the SSH session you already trust, rather than trusting a
first scan. The name in that line must be exactly the `DEPLOY_HOST` value (here the IP):

```bash
laptop$ ssh -i ~/.ssh/pbip-oci-admin pbip@<vm-ip> 'cat /etc/ssh/ssh_host_ed25519_key.pub' | \
          awk -v h=<vm-ip> '{print h" "$1" "$2}' > /tmp/pbip_known_hosts
laptop$ gh api -X PUT repos/rachitst/pbip/environments/staging
laptop$ gh secret set DEPLOY_HOST --env staging --body "<vm-ip>"
laptop$ gh secret set DEPLOY_USER --env staging --body "pbip"
laptop$ gh secret set DEPLOY_SSH_KEY --env staging < ~/.ssh/pbip-deploy
laptop$ gh secret set DEPLOY_KNOWN_HOSTS --env staging < /tmp/pbip_known_hosts && rm /tmp/pbip_known_hosts
laptop$ gh variable set STAGING_URL --env staging --body "https://<host>"
laptop$ gh variable set DEPLOY_ENABLED --body true
```

Then, for #198:

```bash
laptop$ gh workflow run deploy.yml -f tag=<vX.Y.Z> -f dry_run=true     # tag check, image build, secret check, plan
laptop$ gh workflow run deploy.yml -f tag=<vX.Y.Z>                     # real: copy files, deploy.sh, smoke test
laptop$ gh run list --workflow deploy.yml --limit 3                    # record the run URLs on #198
```

Rollback proof (needs two healthy releases on the host): deploy `<vA>`, then `<vB>`, then
`vm$ cd ~/pbip && bash deploy.sh rollback` and check `cat ~/pbip/.deployed_tag` shows `<vA>` and the smoke tests pass.
Record the outputs on #198 (runbook section 6).

## 14. Data on staging: empty, not demo

* `scripts/seed_dev.py` (demo customers, staff, MFA) and `scripts/seed_load.py` (load-test users) **refuse** a
  database whose `APP_ENV` is `staging` or `production`, and the production image does not contain `scripts/`. This is
  deliberate: the demo passwords and fixed MFA secrets must never guard a deployed environment.
* Staging therefore starts empty. Customers register through the web app (self-registration is on unless
  `ALLOW_SELF_REGISTRATION=false`, and the production compose file does not set it); new accounts open with a zero
  balance.
* **Gaps to know about** (not fixed by this guide): there is no supported way to create the first staff
  administrator, or to fund an account, in a deployed environment. Staff screens and money movement can therefore not
  be demonstrated on staging today; the demo stays on the local stack. Closing that gap is an engineering change with
  security impact (a one-time admin bootstrap) and needs its own issue and ADR.
* The production compose file runs the rules-only fraud scorer (`FRAUD_SCORER` defaults to `rules` and no model
  directory is mounted).

## 15. Backups to OCI Object Storage

The backup procedure is runbook section 4.1 unchanged (`scripts/backup.py`, encrypted with gpg, verified by
`scripts/restore.py`). It needs a checkout of the repository at the deployed tag on the host, the loopback override and
`gpg`. One-time set-up:

```bash
vm$ sudo apt-get install -y git gpg python3-venv
vm$ curl -LsSf https://astral.sh/uv/0.11.7/install.sh | sh                 # same uv version as CI
vm$ export PATH="$HOME/.local/bin:$PATH"     # uv's install directory; scripts/dev.py calls `uv` from PATH
vm$ git clone https://github.com/rachitst/pbip.git ~/pbip-repo && cd ~/pbip-repo && git checkout <vX.Y.Z>
vm$ uv sync --all-packages --frozen
vm$ printf 'services:\n  db:\n    ports: ["127.0.0.1:5432:5432"]\n' > ~/pbip/docker-compose.prod.backup.yml
vm$ sudo install -d -m 700 -o pbip -g pbip /var/backups/pbip
vm$ python3 -c "import secrets; print(secrets.token_urlsafe(48))" > ~/.pbip-backup-passphrase && chmod 600 ~/.pbip-backup-passphrase
```

(The runbook keeps the passphrase in `/root`; here the backup runs as `pbip`, so it lives in `pbip`'s home with mode
600. Copy it into your password manager now: a lost passphrase is a lost backup.)

Each backup (daily, and before every migration or secret rotation):

```bash
vm$ cd ~/pbip && docker compose -f docker-compose.prod.yml -f docker-compose.prod.backup.yml up -d db
vm$ export PATH="$HOME/.local/bin:$PATH"
vm$ cd ~/pbip-repo && set -a && . ~/pbip/.env && set +a
vm$ export DATABASE_URL="postgresql+psycopg://$POSTGRES_USER:$POSTGRES_PASSWORD@127.0.0.1:5432/$POSTGRES_DB"
vm$ DB=$(docker compose -f ~/pbip/docker-compose.prod.yml ps -q db)
vm$ uv run python scripts/backup.py --out-dir /var/backups/pbip \
      --passphrase-file ~/.pbip-backup-passphrase --docker-container "$DB"
vm$ cd ~/pbip && docker compose -f docker-compose.prod.yml up -d db       # unpublish 5432 again
```

**Off-host copy.** A backup that exists only on the VM it protects is not a backup. Object Storage is part of the
free tier (check the current allowance; the encrypted dump of the local demo database was about 12 MB, runbook
section 4.2).

1. Storage > Buckets > **Create bucket** `pbip-backups` (Standard tier, private, with **Object Versioning** enabled),
   and add a lifecycle rule that deletes objects after the retention you choose.
2. Let the VM write without storing an API key on it, using an instance principal: Identity > Dynamic groups >
   create `pbip-staging-hosts` with the matching rule `instance.id = '<instance OCID>'`; then Identity > Policies,
   in the bucket's compartment:

   ```text
   Allow dynamic-group pbip-staging-hosts to read buckets in compartment <compartment> where target.bucket.name = 'pbip-backups'
   Allow dynamic-group pbip-staging-hosts to manage objects in compartment <compartment> where all {target.bucket.name = 'pbip-backups', any {request.permission = 'OBJECT_CREATE', request.permission = 'OBJECT_INSPECT'}}
   ```

   The second statement is write-only (create and list, no read, overwrite or delete), so a compromised host can add
   backups but cannot read, replace or delete earlier ones.

   (On tenancies with identity domains, the dynamic group is created inside the `Default` domain.)
3. Install the OCI CLI for `pbip` in its own virtual environment and upload the newest dump and manifest:

   ```bash
   vm$ python3 -m venv ~/oci-cli && ~/oci-cli/bin/pip install -q oci-cli
   vm$ NS=$(~/oci-cli/bin/oci os ns get --auth instance_principal --query data --raw-output)
   vm$ for f in $(ls -t /var/backups/pbip/*.dump.gpg | head -1) $(ls -t /var/backups/pbip/*.manifest.json | head -1); do
         ~/oci-cli/bin/oci os object put --auth instance_principal --namespace-name "$NS" \
           --bucket-name pbip-backups --file "$f" --name "$(basename "$f")"; done
   ```

The alternative of a second block volume stays on the same host and the same failure domain; use it only as extra
local space, not as the off-host copy. Run the restore drill (runbook section 4.2) on the host once a month and record
`PASS` on the operations issue.

## 16. Monitoring on staging

The Prometheus, Grafana, Loki and blackbox stack (runbook section 1.5) is the **`monitoring` profile of the local
`docker-compose.yml`**; the production compose file has no monitoring services, and that profile's scrape targets are
the local stack's containers. Running it against `pbip-prod` needs an engineering change (an overlay that joins the
production network and a production Prometheus config), which is not part of this guide. What staging has:

* container logs: `vm$ docker compose -f ~/pbip/docker-compose.prod.yml logs --since 1h api` (JSON, redacted);
* the availability probe and report below (TASK-103);
* `unhandled_exception` events with `request_id` and route template in the API logs (runbook section 1.5, "Error
  tracking"): `vm$ docker compose -f ~/pbip/docker-compose.prod.yml logs api | grep '"unhandled_exception"'`.

## 17. Availability probe (TASK-103)

`infra/deploy/uptime-probe.sh` makes one request to `https://<host>/api/health/ready` (nginx TLS, then the API, which
checks PostgreSQL and Redis) and appends `<UTC time> <up|down> <HTTP status> <seconds>` to a log. A systemd timer runs
it every 30 seconds. `PROBE_CONNECT_TO=::127.0.0.1:` makes curl connect to nginx on the VM itself while still
verifying the certificate for `<host>`, so the probe does not depend on the security list or on the VM reaching its
own public address. It therefore measures **the stack**, not your internet path. When the VM itself is down, no line
is written; `scripts/uptime_report.py` counts every missing probe as down in its conservative figure.

Install (as `ubuntu`, which has sudo). With option B, set `HOST=<vm-ip>` and add the line
`Environment=PROBE_CACERT=/home/pbip/pbip/certs/staging-ca.crt` under `[Service]` (the CA copied in section 9.2):

```bash
vm$ HOST=<host>
vm$ sudo tee /etc/systemd/system/pbip-uptime-probe.service >/dev/null <<EOF
[Unit]
Description=PBIP staging availability probe (TASK-103)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
User=pbip
Environment=PROBE_CONNECT_TO=::127.0.0.1:
ExecStart=/bin/bash /home/pbip/pbip/uptime-probe.sh https://${HOST}/api/health/ready /home/pbip/pbip/uptime/probe.log
EOF
vm$ sudo tee /etc/systemd/system/pbip-uptime-probe.timer >/dev/null <<'EOF'
[Unit]
Description=Run the PBIP availability probe every 30 seconds

[Timer]
OnBootSec=30s
OnUnitActiveSec=30s
AccuracySec=1s

[Install]
WantedBy=timers.target
EOF
vm$ sudo systemctl daemon-reload && sudo systemctl enable --now pbip-uptime-probe.timer
vm$ systemctl list-timers pbip-uptime-probe.timer
vm$ tail -3 /home/pbip/pbip/uptime/probe.log          # a line every 30 s: "... up 200 0.0xx"
```

Install the probe once the real certificate is in place (section 9): against the placeholder certificate every
probe fails verification and is logged as down. Note the installation time; reports should start there (`--since`), because the conservative figure counts the time
before the first probe as missing.

**Weekly report** (and the TASK-103 "measured for at least one week" evidence):

```bash
vm$ python3 ~/pbip/uptime_report.py ~/pbip/uptime/probe.log --days 7
vm$ python3 ~/pbip/uptime_report.py ~/pbip/uptime/probe.log --since 2026-12-01T00:00:00Z --until 2027-01-01T00:00:00Z
vm$ python3 ~/pbip/uptime_report.py ~/pbip/uptime/probe.log --days 30 --min-availability 99   # exit 1 below 99%
```

It prints the probes that ran and were expected, the observed and the conservative availability, every incident
(consecutive failed probes, with start, end, duration and HTTP statuses) and every gap. The **conservative** figure is
the one to report against the 99% NFR. To have the report written every Monday, with the unit marked failed when the
week is below 99% (visible in `systemctl --failed`; there is no notification channel, runbook section 1.5):

```bash
vm$ sudo tee /etc/systemd/system/pbip-uptime-report.service >/dev/null <<'EOF'
[Unit]
Description=PBIP weekly availability report (TASK-103)

[Service]
Type=oneshot
User=pbip
ExecStart=/bin/sh -c '/usr/bin/python3 /home/pbip/pbip/uptime_report.py /home/pbip/pbip/uptime/probe.log --days 7 --min-availability 99 > /home/pbip/pbip/uptime/report-$(date -u +%%Y%%m%%d).txt'
EOF
vm$ sudo tee /etc/systemd/system/pbip-uptime-report.timer >/dev/null <<'EOF'
[Unit]
Description=Weekly PBIP availability report

[Timer]
OnCalendar=Mon *-*-* 03:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
EOF
vm$ sudo systemctl daemon-reload && sudo systemctl enable --now pbip-uptime-report.timer
```

The log grows by about 2,880 short lines a day (well under 1 MB a week); keep it for the reporting period.

## 18. Performance runs against the cloud host (TASK-078, TASK-079, TASK-102)

### 18.1 Page-load and login budgets against staging, from the laptop (TASK-102)

The Playwright budgets (`apps/web/e2e/perf.spec.ts`: page load < 2 s, login < 3 s) run unchanged against staging. The
spec registers one new customer per run (`e2e-perf-<id>@example.com`, through the normal API; registration is
limited to 10 per hour per IP), so staging collects one test customer per run.

```bash
laptop$ cd apps/web && npm ci && npx playwright install chromium
laptop$ mkdir -p ../../perf-results
laptop$ E2E_BASE_URL=https://<host> PLAYWRIGHT_JSON_OUTPUT_NAME=../../perf-results/page-load-staging.json \
          npx playwright test e2e/perf.spec.ts --reporter=list,json
```

These numbers include your internet path to the region; record where you ran from (city, connection) with them. The
Playwright configuration ignores certificate errors, so option B works as well.

### 18.2 The k6 load suite on the VM (TASK-078, TASK-102 API budgets, TASK-079)

The load suite cannot target staging itself, by design: its users come from `scripts/seed_load.py`, which refuses a
staging database, and all virtual users share one IP, which staging's per-IP login limit (10 per minute) would throttle.
Raising those limits on a deployed environment would weaken a control. Instead, run the same procedure as the
`Performance` workflow (`.github/workflows/perf.yml`), on the VM, against a separate local-mode stack (compose project
`pbip`, `APP_ENV=local`, own volumes; nothing is shared with `pbip-prod`). You drive it over SSH from the laptop; the
load generator runs on the VM, as in CI, so the network is not measured. The staging stack keeps running (it is idle);
its CPU use is small but it shares the VM, so note it with the results.

```bash
laptop$ ssh -i ~/.ssh/pbip-oci-admin pbip@<vm-ip>
vm$ git clone https://github.com/rachitst/pbip.git ~/pbip-perf && cd ~/pbip-perf && git checkout <vX.Y.Z-or-main>
vm$ export PATH="$HOME/.local/bin:$PATH"       # uv, installed in section 15
vm$ uv sync --all-packages --frozen
vm$ python3 scripts/init_env.py                             # local-only .env with fresh secrets
vm$ uv run python scripts/dev_certs.py
vm$ uv run python scripts/dev.py fraud-model   # the hybrid scorer the suite measures
vm$ export API_REPLICAS=2                                    # production topology (ADR-0045, once merged)
vm$ docker compose -f docker-compose.yml -f infra/k6/compose.load.yml up -d --build
vm$ curl --fail --silent --retry 60 --retry-delay 2 --retry-all-errors --cacert infra/certs/ca.crt https://localhost:8443/api/health/ready
vm$ mkdir -p perf-results
vm$ uv run python scripts/seed_load.py --users 100
vm$ uv run python scripts/load_invariants.py snapshot > perf-results/invariants-before.json
vm$ { nproc; lscpu | grep -E 'Model name|Thread|Core'; free -h | head -2; uptime; docker compose ps -q api | wc -l; } | tee perf-results/environment.txt
vm$ BASE_URL=https://localhost:8443 USERS=100 VUS=50 DURATION=2m python3 scripts/dev.py load --summary-export /results/k6-summary.json
vm$ uv run python scripts/load_invariants.py check perf-results/invariants-before.json | tee perf-results/invariants-after.json
laptop$ scp -r -i ~/.ssh/pbip-oci-admin pbip@<vm-ip>:pbip-perf/perf-results ./perf-results-oci-a1
```

`VUS`, `DURATION` and `USERS` are the workflow's defaults; raise `VUS` toward the TASK-078 target (500) in steps and
keep every run's `environment.txt`. The k6 thresholds are the NFR budgets (p95 < 500 ms per route group, login < 3 s,
errors < 0.5%), and `load_invariants.py` proves no overdraft, balanced ledger and no new deadlocks.

TASK-079 (slow statements under load): once ADR-0045's PR (#419) is merged, `infra/k6/compose.load.yml` loads
`pg_stat_statements`, and `.github/workflows/perf.yml` shows the exact reset-before and report-after commands
(`top-statements.sql` in `infra/k6/`); run the same two commands around the k6 step above.

When finished, remove the perf stack and its volumes. This names the perf project explicitly and cannot touch
`pbip-prod`. **Run it only on the VM**: on your laptop, project `pbip` is your local demo stack and `-v` would
delete its database.

```bash
vm$ cd ~/pbip-perf && docker compose -p pbip -f docker-compose.yml -f infra/k6/compose.load.yml down -v
```

## 19. The other #198 checks on this host

* **GeoIP (#198 item 2):** fetch and mount the databases as in runbook section 2.4 and confirm the API starts with
  them. The `NEW_COUNTRY` transfer check needs a funded account, which staging cannot create (section 14), so do that
  part on the local stack, as #198 already suggests.
* **Celery beat at its scheduled times (#198 item 3):** leave staging up over 02:00 UTC and a Sunday 03:00 UTC, then:

  ```bash
  vm$ cd ~/pbip && docker compose -f docker-compose.prod.yml logs --since <date>T01:55:00Z --until <date>T02:10:00Z worker | grep spending_materialized
  vm$ docker compose -f docker-compose.prod.yml logs --since <sunday>T02:55:00Z --until <sunday>T03:15:00Z worker | grep -i segment
  vm$ docker compose -f docker-compose.prod.yml ps beat           # exactly one beat container
  ```

* **Cutover downtime (#198 item 1, follow-up):** during a deploy, the probe log (section 17) records any failed or
  missing probes; quote the incident list for the deploy window rather than claiming zero downtime.

## 20. Costs and teardown

Everything above stays inside the Always Free limits as they stood at the time of writing (check current free-tier
limits). To remove the host: terminate the instance (and its boot volume), delete the bucket, the dynamic group and
policy, the reserved IP if any, and on GitHub remove the `staging` environment and the `DEPLOY_ENABLED` variable.
Revoke the GHCR token and remove `pbip-deploy.pub` from wherever it was used.
