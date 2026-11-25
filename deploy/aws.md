# Running Cutover on AWS EC2 with a Least-Privilege IAM Role

This runbook shows how to run Cutover's full validation, mapping, reconciliation, and verified S3 upload pipeline on an **Amazon Linux 2023** EC2 instance using an **IAM instance profile** — with zero static AWS access keys stored on the machine or in this repository.

## Why This Architecture

- **No static credentials:** `upload_s3.py` uses `boto3`'s default credential chain. On EC2 with an attached IAM role, `boto3` automatically retrieves short-lived temporary credentials from the Instance Metadata Service (IMDSv2). No `~/.aws/credentials` file or `.env` key is created.
- **Least-privilege S3 policy ([iam-policy.json](iam-policy.json)):**
  - `s3:PutObject` and `s3:GetObject` are scoped strictly to `arn:aws:s3:::<BUCKET_NAME>/cutover-runs/*`. In AWS IAM, `s3:GetObject` authorizes both `GetObject` (download-back SHA-256 verification) and `HeadObject` (overwrite check).
  - `s3:ListBucket` is granted on `arn:aws:s3:::<BUCKET_NAME>` so S3 returns `404 NotFound` (rather than `403 AccessDenied`) when `upload_s3.py` runs `HeadObject` on a brand-new key that does not exist yet.
  - `s3:DeleteObject`, writes outside `cutover-runs/*`, and access to any other bucket are denied by default.

## 1. AWS Prerequisites

Create these resources in your AWS account (for example in `us-east-2` or `us-east-1`), replacing `<BUCKET_NAME>` with a globally unique bucket name:

1. **Private S3 bucket:** Block all public access enabled; default server-side encryption (SSE-S3) enabled.
2. **IAM policy (`cutover-ec2-s3-prefix`):** Paste [iam-policy.json](iam-policy.json) with `<BUCKET_NAME>` substituted.
3. **IAM role & instance profile (`cutover-ec2-role`):** Use [ec2-trust-policy.json](ec2-trust-policy.json) as the trust policy and attach `cutover-ec2-s3-prefix`.
4. **Security group (`cutover-ssh-only`):** Inbound TCP port `22` restricted to your current public IP (`/32`) only; default outbound HTTPS (`443`) allowed for GitHub and S3 endpoints.
5. **EC2 instance:**
   - AMI: Amazon Linux 2023 (64-bit x86)
   - Instance type: `t3.micro`
   - IAM instance profile: `cutover-ec2-role`
   - Metadata options: **IMDSv2 required** (`HttpTokens=required`)
   - Key pair: `.pem` file kept locally outside the repo (`.gitignore` blocks `*.pem`).

## 2. Run Cutover Over SSH (Amazon Linux 2023)

Connect over SSH:

```bash
chmod 400 cutover-ec2.pem
ssh -i cutover-ec2.pem ec2-user@<EC2_PUBLIC_IP>
```

On the EC2 instance, confirm the instance role is active (no static keys), clone the repository, and run the full pipeline on the reviewed synthetic AppFolio-style tenant file:

```bash
# 1. Confirm the instance is using the IAM role (not static keys)
aws sts get-caller-identity

# 2. Install git and Python 3.11 (Amazon Linux 2023 ships Python 3.9 by default; Cutover requires 3.10+)
sudo dnf install -y git python3.11
git clone https://github.com/MichaelMartiradonna/cutover.git
cd cutover

# 3. Create a virtual environment and install dependencies
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip pyyaml boto3

# 4. Run the full Cutover pipeline (validate -> map -> reconcile)
RUN_DIR="output/ec2-$(date -u +%Y%m%d-%H%M%S)"
python validate.py data/examples/tenants_appfolio_200_reviewed.csv --rules rules/tenants.yaml --out "$RUN_DIR"
python map.py "$RUN_DIR/clean.csv" --mapping mappings/tenants_appfolio_to_import.yaml --out "$RUN_DIR"
python reconcile.py "$RUN_DIR/clean.csv" "$RUN_DIR/target.csv" --key unit,name --mapping mappings/tenants_appfolio_to_import.yaml --out "$RUN_DIR"

# 5. Run verified S3 upload under the allowed prefix
BUCKET="<BUCKET_NAME>"
KEY="cutover-runs/$(basename "$RUN_DIR")/target.csv"
python upload_s3.py "$RUN_DIR/target.csv" "s3://$BUCKET/$KEY" --out "$RUN_DIR"
cat "$RUN_DIR/upload_report.md"
```

Expected result: every stage exits `0`, `reconciliation.md` ends with `Verdict: PASS`, and `upload_report.md` ends with `Verdict: PASS` confirming the downloaded S3 object matches `target.csv.sha256` byte for byte.

## 3. Verify Least-Privilege Boundaries (Negative Tests)

Run these checks from the same EC2 SSH session to confirm the IAM role rejects unauthorized actions:

```bash
# Denied: writing outside the cutover-runs/ prefix
aws s3 cp "$RUN_DIR/target.csv" "s3://$BUCKET/outside-prefix/target.csv"
# Expected: An error occurred (AccessDenied) when calling the PutObject operation

# Denied: deleting the uploaded object
aws s3 rm "s3://$BUCKET/$KEY"
# Expected: delete failed: ... AccessDenied
```

## 4. Teardown (Same-Day Cost Control)

Exit SSH and terminate the EC2 instance immediately after verification so compute charges stop:

```bash
exit
```

From your workstation (or the AWS Console):
1. Terminate the EC2 instance and confirm its state reaches `terminated`.
2. Delete the temporary security group and local `.pem` key pair if no longer needed.
3. Empty and delete the test S3 bucket (or keep it empty with zero objects) and remove the IAM role/policy if this was a one-off verification run.

## 5. Verified Run Log (2026-11-24, `us-east-2`)

Executed over SSH on a fresh `t3.micro` running **Amazon Linux 2023** (`Linux 6.18`, `Python 3.11.18`) with **IMDSv2 required**, SSH restricted to `/32`, and the least-privilege IAM instance profile from [iam-policy.json](iam-policy.json) and [ec2-trust-policy.json](ec2-trust-policy.json) (account IDs and temporary resource identifiers redacted):

| Check | Command / Action | Result |
|---|---|---|
| No static keys on disk | `ls -la ~/.aws` | `No such file or directory` |
| IAM role active via IMDSv2 | `aws sts get-caller-identity` | `arn:aws:sts::<ACCOUNT_ID>:assumed-role/cutover-ec2-role/...` |
| Validate → Map → Reconcile | `validate.py`, `map.py`, `reconcile.py` on `tenants_appfolio_200_reviewed.csv` | All exited `0` · `Verdict: PASS (14 checks, 0 failed)` |
| Verified S3 upload (allowed prefix) | `upload_s3.py` → `s3://<BUCKET>/cutover-runs/.../target.csv` | Exit `0` · `Verdict: PASS` (`11,179` bytes local & remote; sidecar, local, and remote SHA-256 all `49ec8e23ecd9cf63abe416d9500e14f90a987e00f688e49b81be516112c46c46`) |
| Overwrite refusal | Second `upload_s3.py` call to the same S3 key | Exit `2` · `Refusing to overwrite existing object` |
| Negative IAM test: outside prefix | `aws s3 cp ... s3://<BUCKET>/outside-prefix/target.csv` | Exit `1` · `AccessDenied` (`s3:PutObject` not allowed outside `cutover-runs/*`) |
| Negative IAM test: delete object | `aws s3 rm s3://<BUCKET>/cutover-runs/.../target.csv` | Exit `1` · `AccessDenied` (`s3:DeleteObject` not allowed) |
| Teardown | Terminate EC2 instance and delete temporary SG, key pair, bucket, and IAM role/policy | Confirmed EC2 state `terminated`; all temporary resources removed the same day |
