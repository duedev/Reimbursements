# Deploying free on Oracle Cloud "Always Free"

Run the app 24/7 for **$0/month** on an Oracle Cloud Always-Free ARM VM, with
automatic HTTPS via Caddy. The Always-Free Ampere A1 allowance (up to 4 cores /
24 GB RAM / 200 GB storage) is free for the life of the account — not a trial.

You build the image **on the VM**, so the ARM (`aarch64`) wheels for onnxruntime,
OpenCV, Pillow, and PyMuPDF are pulled automatically — there's nothing to
cross-compile.

> **The local LM Studio tier is inert in the cloud** (there's no local model to
> reach), so the extraction chain is effectively **Gemini → Mistral → offline
> parser**. Set at least one cloud key (both are free tiers) for LLM extraction.

---

## 1. Create the VM

1. Sign up at <https://www.oracle.com/cloud/free/> (a card is required for identity
   verification — Always-Free resources are never charged).
2. **Compute → Instances → Create instance:**
   - **Image:** Canonical Ubuntu 24.04 (or 22.04).
   - **Shape:** change to **Ampere (Arm)** → `VM.Standard.A1.Flex`. 2 OCPU / 12 GB
     RAM is plenty and stays inside the free allowance.
   - **Add your SSH public key** (you'll need it to log in).
   - Create. Note the **public IP**.

> Free ARM capacity is popular and can return an "out of capacity" error — just
> retry the create a few times, or pick a different Availability Domain.

## 2. Open ports 80 and 443

Oracle blocks inbound traffic in **two** places — you must open both:

**a) VCN Security List** (in the console): Networking → your VCN → the subnet's
Security List → **Add Ingress Rules**: source `0.0.0.0/0`, TCP, destination ports
**80** and **443**.

**b) The instance firewall** (over SSH — Oracle's Ubuntu image ships restrictive
iptables):

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80  -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

## 3. Install Docker

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER && newgrp docker   # run docker without sudo
```

## 4. Point a hostname at the VM

Caddy needs a real hostname (for the free TLS cert) resolving to the VM's public
IP. If you don't own a domain, get a free subdomain in 30 seconds at
<https://www.duckdns.org> (e.g. `my-receipts.duckdns.org`) and point it at your
VM's public IP.

## 5. Get the code and configure

```bash
git clone https://github.com/duedev/Reimbursements.git
cd Reimbursements
git checkout claude/dazzling-fermat-qkv4dt

cat > .env <<'EOF'
APP_AUTH_TOKEN=replace-with-a-long-random-string
APP_DOMAIN=my-receipts.duckdns.org
GEMINI_API_KEY=your-free-gemini-key
MISTRAL_API_KEY=your-free-mistral-key
EOF
```

Generate a strong token with `openssl rand -hex 24`. The cloud keys are optional
but at least one is needed for LLM extraction (both have free tiers — see the
AI Models settings card for details).

## 6. Launch

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

First build takes a few minutes (it compiles the OCR stack and smoke-tests
RapidOCR). Then Caddy fetches a Let's Encrypt certificate automatically.

Visit **`https://<APP_DOMAIN>/?token=<APP_AUTH_TOKEN>`** once — that drops the
auth cookie so the SPA's API/SSE calls authenticate from then on.

---

## Operating it

| Task | Command (run from the repo dir) |
|---|---|
| Logs | `docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f` |
| Update to latest | `git pull && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build` |
| Stop | `docker compose -f docker-compose.yml -f docker-compose.prod.yml down` |
| Status | `docker compose -f docker-compose.yml -f docker-compose.prod.yml ps` |

**Persistence.** App config, the secrets store (API keys set in the UI), reports,
and the receipt working folders live in the bind-mounted `./intake`, `./output`,
`./export`, and `./config` directories on the VM, and Caddy's certificates in a
named volume — all survive restarts, rebuilds, and `down`/`up`.

**Security.** `APP_AUTH_TOKEN` is the only thing standing between the public
internet and your receipts — keep it long and secret, and never commit `.env`.
The `.env` file and the `./config` secrets store stay on the VM only.

**Cost.** Staying within the Always-Free Ampere A1 allowance (≤ 4 OCPU / 24 GB
RAM / 200 GB block storage, 1 VM) and the cloud LLM **free** tiers keeps this at
$0/month indefinitely.
