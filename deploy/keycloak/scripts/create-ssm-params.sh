#!/bin/bash
set -e

# Default values
AWS_REGION="${AWS_REGION:-us-east-2}"
SSM_PREFIX="${SSM_PREFIX:-/keycloak/db}"

# Generate secure password if not provided
DB_USERNAME="${DB_USERNAME:-keycloak}"
DB_PASSWORD="${DB_PASSWORD:-$(openssl rand -base64 32)}"

echo "Creating AWS SSM parameters in region: $AWS_REGION"
echo "Parameter prefix: $SSM_PREFIX"

# Create username parameter
aws ssm put-parameter \
  --region "$AWS_REGION" \
  --name "${SSM_PREFIX}/username" \
  --value "$DB_USERNAME" \
  --type String \
  --overwrite

echo "✓ Created ${SSM_PREFIX}/username"

# Create password parameter
aws ssm put-parameter \
  --region "$AWS_REGION" \
  --name "${SSM_PREFIX}/password" \
  --value "$DB_PASSWORD" \
  --type SecureString \
  --overwrite

echo "✓ Created ${SSM_PREFIX}/password"
echo ""
echo "SSM parameters created successfully!"
echo "Username: $DB_USERNAME"
echo "Password: [stored in SSM as SecureString]"
