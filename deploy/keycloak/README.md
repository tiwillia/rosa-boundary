# Keycloak Deployment for OpenShift

Self-contained Keycloak deployment using Red Hat Build of Keycloak (RHBK) operator, CloudNativePG for database, and External Secrets for AWS SSM integration.

## Table of Contents

- [Usage](#usage)
  - [Prerequisites](#prerequisites)
  - [Deployment Steps](#deployment-steps)
  - [Verification](#verification)
  - [Getting Credentials](#getting-credentials)
  - [Customization](#customization)
- [What is Created](#what-is-created)
  - [Operators (Cluster-Scoped)](#operators-cluster-scoped)
  - [Application Resources (keycloak namespace)](#application-resources-keycloak-namespace)
  - [Architecture](#architecture)
  - [SecretStore Comparison](#secretstore-comparison)
- [Troubleshooting](#troubleshooting)
- [Cleanup](#cleanup)

## Usage

### Prerequisites

1. **OpenShift cluster** (ROSA or OpenShift 4.14+)
2. **AWS credentials** with permissions to:
   - Create/read SSM parameters
   - Assume IAM roles (for IRSA/service accounts)
3. **kubectl or oc CLI** installed locally
4. **AWS CLI** installed (for SSM parameter creation)

### Deployment Steps

This deployment follows a three-phase approach:

#### Phase 1: Install Cluster-Wide Operators (One-time per cluster)

Install CloudNativePG and External Secrets operators cluster-wide. The RHBK operator will be installed namespace-scoped in Phase 3 because it doesn't support AllNamespaces mode.

```bash
# Install cluster-wide operators (CloudNativePG and External Secrets)
oc apply -k operators/overlays/stable

# Wait for operators to be ready (2-5 minutes)
oc get csv -n openshift-operators | grep -E 'cloudnative-pg|external-secrets'
```

**Expected output:**
```
cloudnative-pg.v1.28.0                CloudNativePG             1.28.0    Succeeded
external-secrets-operator.v0.11.0     External Secrets Operator 0.11.0    Succeeded
```

**Note**: RHBK operator is installed namespace-scoped during application deployment (Phase 3).

#### Phase 2: Create AWS SSM Parameters (One-time per environment)

Database credentials are stored in AWS Systems Manager Parameter Store.

```bash
cd deploy/keycloak

# Use defaults (us-east-2, /keycloak/db/*)
./scripts/create-ssm-params.sh

# OR customize region and prefix
AWS_REGION=us-west-2 SSM_PREFIX=/my-keycloak/db ./scripts/create-ssm-params.sh

# OR provide custom credentials
DB_USERNAME=admin DB_PASSWORD=mypass123 ./scripts/create-ssm-params.sh
```

**Script creates:**
- `/keycloak/db/username` - Database username (String)
- `/keycloak/db/password` - Database password (SecureString)

#### Phase 3: Deploy Keycloak Application

This will deploy the application resources and install the RHBK operator namespace-scoped.

```bash
# Deploy to dev environment (includes RHBK operator)
oc apply -k overlays/dev

# Wait for RHBK operator to install (~1-2 minutes)
oc get csv -n keycloak | grep rhbk

# Monitor deployment
oc get pods -n keycloak -w
```

**Wait for all pods to be Running:**
```
NAME                                   READY   STATUS    RESTARTS   AGE
keycloak-db-1                          1/1     Running   0          2m
keycloak-0                             1/1     Running   0          1m
rhbk-operator-xxxxx                    1/1     Running   0          2m
```

**⚠️ IRSA Configuration Required**: If External Secrets fails to sync the database secret (check `oc get externalsecret -n keycloak`), you need to configure IRSA or manually create the secret:

```bash
# Temporary workaround: Manually create database secret from SSM
DB_USER=$(aws ssm get-parameter --region us-east-2 --name /keycloak/db/username --query 'Parameter.Value' --output text)
DB_PASS=$(aws ssm get-parameter --region us-east-2 --name /keycloak/db/password --with-decryption --query 'Parameter.Value' --output text)
oc create secret generic keycloak-db-app -n keycloak \
  --from-literal=username="$DB_USER" \
  --from-literal=password="$DB_PASS" \
  --type=kubernetes.io/basic-auth
```

See [IRSA Configuration](#irsa-configuration-for-external-secrets) section for permanent solution.

### Verification

```bash
# Check Keycloak CR status
oc get keycloak -n keycloak
# Should show: keycloak   https://keycloak-keycloak.apps...   True

# Check PostgreSQL cluster
oc get cluster -n keycloak
# Should show: keycloak-postgresql   18.1   3   1      Ready

# Check External Secrets
oc get externalsecret -n keycloak
# Should show: keycloak-db-secret   SecretSynced   True

# Test Keycloak URL
oc get route keycloak -n keycloak -o jsonpath='{.spec.host}'
# Visit https://<hostname> in browser
```

### Getting Credentials

Keycloak operator auto-generates admin credentials on first deployment:

```bash
# Get admin username
oc get secret keycloak-initial-admin -n keycloak \
  -o jsonpath='{.data.username}' | base64 -d && echo

# Get admin password
oc get secret keycloak-initial-admin -n keycloak \
  -o jsonpath='{.data.password}' | base64 -d && echo

# Get Keycloak URL
oc get route keycloak -n keycloak -o jsonpath='{.spec.host}' && echo
```

**Login:**
1. Navigate to `https://<hostname>`
2. Click "Administration Console"
3. Use credentials from above

### Customization

#### Creating Additional Environments

To create a new environment (e.g., `staging`):

```bash
cd deploy/keycloak/overlays
cp -r dev staging

# Edit staging/environment-config.yaml
# Update AWS_REGION, AWS_ACCOUNT_ID, IRSA_ROLE_NAME

# Edit staging/kustomization.yaml patches if needed
# (e.g., different service account role ARN)

# Deploy
oc apply -k overlays/staging
```

#### Using Different Operator Channels

To use different operator versions:

```bash
cd operators/overlays
cp -r stable testing

# Edit testing/kustomization.yaml
# Change channel values (e.g., 'stable' -> 'alpha')

# Apply
oc apply -k operators/overlays/testing
```

#### Changing SSM Parameter Paths

If using non-default SSM paths:

1. Update `components/cnpg/external-secret-db.yaml`:
   ```yaml
   spec:
     data:
       - secretKey: username
         remoteRef:
           key: /your-custom-path/username  # Update this
       - secretKey: password
         remoteRef:
           key: /your-custom-path/password  # Update this
   ```

2. Run `create-ssm-params.sh` with custom prefix:
   ```bash
   SSM_PREFIX=/your-custom-path ./scripts/create-ssm-params.sh
   ```

### IRSA Configuration for External Secrets

For External Secrets to automatically sync SSM parameters, you must configure IAM Roles for Service Accounts (IRSA) on ROSA.

#### Create IAM Role and Policy

```bash
# Set variables
AWS_REGION="us-east-2"
AWS_ACCOUNT_ID="641875867446"
CLUSTER_NAME="your-cluster-name"
ROLE_NAME="rosa-keycloak-external-secrets"

# Get OIDC provider from ROSA cluster
OIDC_PROVIDER=$(oc get authentication cluster -o jsonpath='{.spec.serviceAccountIssuer}' | sed 's|https://||')

# Create IAM policy for SSM read access
cat > /tmp/external-secrets-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ssm:GetParameter",
        "ssm:GetParameters",
        "ssm:GetParameterHistory"
      ],
      "Resource": "arn:aws:ssm:${AWS_REGION}:${AWS_ACCOUNT_ID}:parameter/keycloak/db/*"
    }
  ]
}
EOF

aws iam create-policy \
  --policy-name ${ROLE_NAME}-policy \
  --policy-document file:///tmp/external-secrets-policy.json

# Create trust policy
cat > /tmp/trust-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::${AWS_ACCOUNT_ID}:oidc-provider/${OIDC_PROVIDER}"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "${OIDC_PROVIDER}:sub": "system:serviceaccount:openshift-operators:external-secrets-operator-controller-manager"
        }
      }
    }
  ]
}
EOF

# Create IAM role
aws iam create-role \
  --role-name ${ROLE_NAME} \
  --assume-role-policy-document file:///tmp/trust-policy.json

# Attach policy to role
aws iam attach-role-policy \
  --role-name ${ROLE_NAME} \
  --policy-arn arn:aws:iam::${AWS_ACCOUNT_ID}:policy/${ROLE_NAME}-policy
```

#### Annotate Service Account

```bash
# Annotate the External Secrets operator service account with IAM role
oc annotate sa external-secrets-operator-controller-manager \
  -n openshift-operators \
  eks.amazonaws.com/role-arn=arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ROLE_NAME}

# Restart External Secrets operator to pick up annotation
oc delete pod -n openshift-operators -l app.kubernetes.io/name=external-secrets
```

#### Verify External Secrets Sync

```bash
# Check ExternalSecret status
oc get externalsecret keycloak-db-app -n keycloak

# Should show:
# NAME              STORE              REFRESH INTERVAL   STATUS         READY
# keycloak-db-app   aws-ssm-keycloak   1h                 SecretSynced   True

# Verify secret was created
oc get secret keycloak-db-app -n keycloak
```

If you previously created the secret manually, delete it first:
```bash
oc delete secret keycloak-db-app -n keycloak
```

External Secrets will recreate it automatically within seconds.

## What is Created

### Operators

#### Cluster-Scoped Operators (openshift-operators namespace)

| Operator | Purpose | Channel | Source | Version |
|----------|---------|---------|--------|---------|
| **cloudnative-pg** | PostgreSQL operator | stable-v1 | certified-operators | 1.28.0 |
| **external-secrets-operator** | AWS SSM integration | stable | community-operators | 0.11.0 |

#### Namespace-Scoped Operators (keycloak namespace)

| Operator | Purpose | Channel | Source | Version | Notes |
|----------|---------|---------|--------|---------|-------|
| **rhbk-operator** | Red Hat Build of Keycloak | stable-v26 | redhat-operators | 26.4.8 | Namespace-scoped (doesn't support AllNamespaces) |

### Application Resources (keycloak namespace)

#### Namespace
- **keycloak** - Isolated namespace for all Keycloak resources

#### Database (CloudNativePG)
- **Cluster CR**: `keycloak-postgresql`
  - PostgreSQL 18.1
  - 1 instance (default)
  - Storage: 10Gi PVC
  - Database: `keycloak`
  - User credentials from AWS SSM

#### Secrets Management
- **ClusterSecretStore**: `aws-ssm-keycloak`
  - Cluster-wide access to AWS SSM Parameter Store
  - Uses IRSA via `external-secrets-operator-controller-manager` service account
  - Region: Configured via environment-config ConfigMap

- **SecretStore**: `aws-ssm` (namespace-scoped)
  - Namespace-isolated access to AWS SSM
  - Uses IRSA via `external-secrets-sa` service account
  - Region: Configured via environment-config ConfigMap

- **ExternalSecret**: `keycloak-db-secret`
  - Syncs `/keycloak/db/username` and `/keycloak/db/password` from SSM
  - Creates Kubernetes Secret: `keycloak-db-secret`
  - Uses: ClusterSecretStore (default)

- **ServiceAccount**: `external-secrets-sa`
  - Annotated with `eks.amazonaws.com/role-arn` for IRSA
  - Used by SecretStore for AWS authentication

#### Keycloak Application
- **Keycloak CR**: `keycloak`
  - Instances: 1
  - Database: PostgreSQL cluster (auto-configured)
  - Hostname: Auto-generated from OpenShift Route
  - Admin credentials: Auto-generated secret `keycloak-initial-admin`

- **Route**: `keycloak`
  - TLS termination: Edge (OpenShift Router terminates TLS)
  - Backend protocol: HTTP
  - Auto-generated hostname: `keycloak-keycloak.apps.<cluster-domain>`

#### Configuration
- **ConfigMap**: `environment-config`
  - AWS_REGION: us-east-2 (default)
  - AWS_ACCOUNT_ID: 641875867446 (dev default)
  - IRSA_ROLE_NAME: dev-keycloak (dev default)
  - Used by Kustomize replacements to inject environment-specific values

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    OpenShift Cluster                        │
│                                                             │
│  ┌────────────────────────────────────────────────────┐   │
│  │           openshift-operators namespace            │   │
│  │                                                     │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────┐ │   │
│  │  │ RHBK         │  │ CloudNativePG│  │ External │ │   │
│  │  │ Operator     │  │ Operator     │  │ Secrets  │ │   │
│  │  └──────────────┘  └──────────────┘  └──────────┘ │   │
│  └────────────────────────────────────────────────────┘   │
│                                                             │
│  ┌────────────────────────────────────────────────────┐   │
│  │              keycloak namespace                     │   │
│  │                                                     │   │
│  │  ┌──────────────┐         ┌──────────────────┐    │   │
│  │  │  Keycloak    │────────>│  PostgreSQL      │    │   │
│  │  │  (RHBK)      │         │  (CNPG)          │    │   │
│  │  │  - Pods: 1   │         │  - Version: 18.1 │    │   │
│  │  │  - HTTP      │         │  - Storage: 10Gi │    │   │
│  │  └──────────────┘         └──────────────────┘    │   │
│  │         ▲                           ▲              │   │
│  │         │                           │              │   │
│  │  ┌──────┴──────┐           ┌────────┴────────┐   │   │
│  │  │   Route     │           │  ExternalSecret │   │   │
│  │  │   (Edge TLS)│           │  (DB creds)     │   │   │
│  │  └─────────────┘           └─────────────────┘   │   │
│  │         ▲                           ▲              │   │
│  └─────────┼───────────────────────────┼──────────────┘   │
│            │                           │                  │
└────────────┼───────────────────────────┼──────────────────┘
             │                           │
     ┌───────┴────────┐         ┌────────┴────────────┐
     │   Users        │         │  AWS SSM Params     │
     │   (Browser)    │         │  /keycloak/db/*     │
     └────────────────┘         └─────────────────────┘
```

### SecretStore Comparison

This deployment includes both **ClusterSecretStore** and **SecretStore** resources. Understanding when to use each is important:

#### ClusterSecretStore (`aws-ssm-keycloak`)

**Scope**: Cluster-wide access

**Use when:**
- Single tenant cluster (all namespaces trusted)
- Shared infrastructure where multiple namespaces need same SSM access
- Centralized secret management for platform services

**Current usage:**
- ExternalSecret `keycloak-db-secret` uses ClusterSecretStore by default
- Operator controller service account (`external-secrets-operator-controller-manager`) provides IRSA authentication

**Benefits:**
- One SecretStore for entire cluster
- Centralized IAM role management
- Simpler configuration for single-tenant scenarios

**Trade-offs:**
- Any namespace can create ExternalSecrets using this store
- Less isolation between teams/environments

#### SecretStore (`aws-ssm`)

**Scope**: Namespace-scoped access (`keycloak` namespace only)

**Use when:**
- Multi-tenant cluster (namespace isolation required)
- Different teams need different AWS permissions
- Compliance requires strict secret access boundaries

**Current usage:**
- Defined but not actively used by ExternalSecrets in this deployment
- Uses namespace-specific service account (`external-secrets-sa`) with IRSA

**Benefits:**
- Strict namespace isolation
- Per-namespace IAM roles
- Better for multi-tenant environments

**Trade-offs:**
- One SecretStore per namespace
- More IAM roles to manage
- Additional service accounts required

#### Switching Between SecretStores

To use namespace-scoped SecretStore instead of ClusterSecretStore:

1. Edit `components/cnpg/external-secret-db.yaml`:
   ```yaml
   spec:
     secretStoreRef:
       kind: SecretStore  # Change from ClusterSecretStore
       name: aws-ssm      # Change from aws-ssm-keycloak
   ```

2. Ensure `external-secrets-sa` service account has correct IRSA role annotation
3. Redeploy: `oc apply -k overlays/dev`

**Recommendation**: Use ClusterSecretStore for single-tenant ROSA clusters. Use SecretStore when multiple teams share the cluster and need isolated AWS permissions.

## Troubleshooting

### Operators not installing

```bash
# Check cluster-wide operator subscriptions
oc get subscription -n openshift-operators

# Check RHBK operator subscription (namespace-scoped)
oc get subscription -n keycloak

# Check install plans
oc get installplan -n openshift-operators
oc get installplan -n keycloak

# Check CSV status
oc get csv -n openshift-operators | grep -E 'cloudnative|external-secrets'
oc get csv -n keycloak | grep rhbk

# Check operator pod logs
oc logs -n openshift-operators -l app.kubernetes.io/name=external-secrets
oc logs -n keycloak -l app.kubernetes.io/name=rhbk-operator
```

### ExternalSecret not syncing

```bash
# Check ExternalSecret status
oc describe externalsecret keycloak-db-secret -n keycloak

# Check ClusterSecretStore status
oc describe clustersecretstore aws-ssm-keycloak

# Verify SSM parameters exist
aws ssm get-parameter --region us-east-2 --name /keycloak/db/username
aws ssm get-parameter --region us-east-2 --name /keycloak/db/password --with-decryption

# Check IRSA service account annotation
oc get sa external-secrets-operator-controller-manager \
  -n external-secrets-operator -o yaml | grep eks.amazonaws.com
```

### PostgreSQL cluster not starting

```bash
# Check cluster status
oc describe cluster keycloak-postgresql -n keycloak

# Check pod events
oc get events -n keycloak --sort-by='.lastTimestamp'

# Check CloudNativePG operator logs
oc logs -n openshift-operators -l app.kubernetes.io/name=cloudnative-pg
```

### Keycloak pod not starting

```bash
# Check Keycloak CR status
oc describe keycloak keycloak -n keycloak

# Check pod logs
oc logs -n keycloak -l app=keycloak

# Verify database secret exists
oc get secret keycloak-db-secret -n keycloak
```

### Route not accessible

```bash
# Check route status
oc get route keycloak -n keycloak

# Verify route hostname resolves
oc get route keycloak -n keycloak -o jsonpath='{.spec.host}' | xargs nslookup

# Check pod readiness
oc get pods -n keycloak -l app=keycloak

# Test from inside cluster
oc run -it --rm debug --image=curlimages/curl --restart=Never -- \
  curl -I http://keycloak-service.keycloak.svc:8080
```

## Cleanup

### Remove Application (Keep Operators)

```bash
# Delete Keycloak application
oc delete -k overlays/dev

# Verify namespace is cleaned up
oc get all -n keycloak
```

### Remove Operators

**WARNING**: Removing cluster-wide operators affects ALL deployments using them.

```bash
# Delete cluster-wide operator subscriptions
oc delete -k operators/overlays/stable

# Remove cluster-wide CSVs (operator versions)
oc delete csv -n openshift-operators -l operators.coreos.com/cloudnative-pg.openshift-operators
oc delete csv -n openshift-operators -l operators.coreos.com/external-secrets-operator.openshift-operators

# Remove namespace-scoped RHBK operator (done automatically when deleting keycloak namespace)
oc delete csv -n keycloak -l operators.coreos.com/rhbk-operator.keycloak
```

### Remove AWS SSM Parameters

```bash
# Delete SSM parameters (cannot be undone for SecureString without recovery window)
aws ssm delete-parameter --region us-east-2 --name /keycloak/db/username
aws ssm delete-parameter --region us-east-2 --name /keycloak/db/password
```

### Complete Cleanup

```bash
# Delete everything in order
oc delete -k overlays/dev
oc delete -k operators/overlays/stable
oc delete namespace keycloak
aws ssm delete-parameter --region us-east-2 --name /keycloak/db/username
aws ssm delete-parameter --region us-east-2 --name /keycloak/db/password
```

---

## Quick Reference

```bash
# Deploy from scratch
oc apply -k operators/overlays/stable
./scripts/create-ssm-params.sh
oc apply -k overlays/dev

# Get admin credentials
oc get secret keycloak-initial-admin -n keycloak \
  -o jsonpath='{.data.username}' | base64 -d && echo
oc get secret keycloak-initial-admin -n keycloak \
  -o jsonpath='{.data.password}' | base64 -d && echo

# Get URL
oc get route keycloak -n keycloak -o jsonpath='{.spec.host}' && echo

# Check status
oc get keycloak,cluster,externalsecret -n keycloak
```
