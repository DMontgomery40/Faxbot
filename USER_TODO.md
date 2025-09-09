# User TODOs for Local Env and Scripts

Purpose: Track items that block automated env checks or could cause confusion when running scripts/tests locally. None of these are required for the current pytest suite (all tests pass), but they improve reliability of scripts and readiness checks.

- Quote FAX_HEADER value in `.env` so shell sourcing works
  - Current: FAX_HEADER=Faxbot Demo
  - Recommended: FAX_HEADER="Faxbot Demo"
  - Why: `scripts/load-env.sh` sources the file; unquoted spaces turn the trailing word into a command and break the script.

- Fix timezone format in `.env`
  - Current: TZ=Denver/America
  - Recommended: TZ=America/Denver
  - Why: Standard tz database format avoids downstream surprises.

- Remove duplicate FAX_BACKEND entries and choose one
  - Current: FAX_BACKEND appears twice (`sip` earlier, `sinch` later). The last one wins, but it's confusing.
  - Recommended: keep a single line with your intended backend.

- Use distinct secrets for AMI vs internal Asterisk→API posts
  - Current values suggest the same secret might be reused.
  - Recommended: set `ASTERISK_AMI_PASSWORD` and `ASTERISK_INBOUND_SECRET` to different strong values.

- When using phaxio backend in production, ensure HTTPS
  - Set `PUBLIC_API_URL` to an HTTPS URL and keep `ENFORCE_PUBLIC_HTTPS=true` (default). For local non‑TLS testing, temporarily set `ENFORCE_PUBLIC_HTTPS=false`.

Notes
- Test status: `pytest` is fully green locally (21 passed). No tests currently require provider credentials beyond defaults.
- I will keep this list updated as I encounter anything that blocks automated runs.

---

# FAXBOT.NET COMMERCIAL SERVICE SETUP - User Action Items

**CRITICAL CONTEXT:** This is for setting up faxbot.net as a **commercial hosted service** (separate from the open-source Faxbot core). You will be operating as a Business Associate handling PHI for healthcare customers. This requires BAAs with all subprocessors and HIPAA-compliant infrastructure.

**Total Estimated Monthly Cost:** $180-250/month (low usage, HIPAA-compliant)
**One-time Setup Cost:** $50-100
**Time to Complete:** 2-3 weeks

## Phase 1: Domain and Basic Infrastructure

### 1.1 Register faxbot.net Domain
**Cost:** $12-15/year
**Link:** https://www.namecheap.com or https://www.godaddy.com
**Action:**
1. Go to Namecheap (recommended for privacy protection included)
2. Search for "faxbot.net" 
3. Add to cart (~$12.98/year for .net)
4. Enable WhoisGuard privacy protection (free with Namecheap)
5. Complete purchase
6. **Important:** Use your business email and real contact info (required for BAA compliance)

**Watch out for:**
- Don't auto-renew expensive add-ons
- Enable 2FA on your registrar account
- Keep registration info current for HIPAA compliance

### 1.2 Set Up AWS Account
**Cost:** $0 initially (free tier), then ~$150-200/month
**Link:** https://aws.amazon.com/free/
**Action:**
1. Create AWS account with business email
2. **Choose Business account type** (required for BAAs)
3. Complete identity verification
4. Set up billing alerts at $50, $100, $200 thresholds
5. Enable MFA on root account immediately
6. Create IAM admin user, disable root access keys

**HIPAA Requirements:**
- Sign AWS Business Associate Agreement (BAA)
- Link: https://aws.amazon.com/compliance/hipaa-eligible-services/
- Enable CloudTrail logging for all regions
- Use only HIPAA-eligible services

### 1.3 Request SSL Certificate
**Cost:** Free (AWS Certificate Manager)
**Action:**
1. Go to AWS Certificate Manager (us-east-1 region for CloudFront)
2. Request public certificate for faxbot.net and *.faxbot.net
3. Choose DNS validation
4. Add CNAME records to your domain (Namecheap DNS)
5. Wait for validation (usually 5-30 minutes)

## Phase 2: Database Infrastructure

### 2.1 Set Up RDS PostgreSQL
**Cost:** $25-35/month (db.t3.micro with 20GB storage)
**AWS Service:** Amazon RDS
**Action:**
1. Go to AWS RDS console
2. Create database
3. Choose PostgreSQL (version 14 or 15)
4. Choose **db.t3.micro** (1 vCPU, 1GB RAM) - cheapest HIPAA-eligible
5. Storage: 20GB GP2 (can grow to 1000GB auto-scaling)
6. **Enable encryption at rest** (required for HIPAA)
7. **Enable backup** (7-day retention minimum)
8. **Enable Multi-AZ** if budget allows (+$25/month for HA)
9. Set master username: `faxbot`
10. Generate strong password (save in password manager)
11. **VPC:** Create new VPC or use default with private subnets
12. **Security group:** Allow PostgreSQL (5432) only from your API instances

**Database URL format:**
```
postgresql+psycopg2://faxbot:PASSWORD@your-db-endpoint:5432/faxbot
```

### 2.2 Database Security Hardening
**Cost:** Included
**Action:**
1. Create database user for application (not master user)
2. Grant minimal privileges: `CONNECT, CREATE, SELECT, INSERT, UPDATE, DELETE` on faxbot database only
3. Enable SSL connections only
4. Set connection timeout limits
5. Enable query logging for audit trail

## Phase 3: Application Hosting

### 3.1 Set Up ECS Fargate for API
**Cost:** $30-50/month (1 task, 0.5 vCPU, 1GB RAM)
**AWS Service:** Amazon ECS with Fargate
**Why Fargate:** Serverless containers, HIPAA-eligible, easier than EC2 management
**Action:**
1. Create ECS cluster
2. Create task definition:
   - **CPU:** 512 (0.5 vCPU)
   - **Memory:** 1024 (1GB)
   - **Image:** Your Faxbot Docker image (pushed to ECR)
3. Set environment variables (see Phase 4)
4. **Enable logging** to CloudWatch
5. Create ECS service with 1 desired task
6. **Enable service discovery** for internal communication
7. Set up Application Load Balancer (ALB) with HTTPS

### 3.2 Set Up ECR for Docker Images
**Cost:** $1-5/month (image storage)
**AWS Service:** Amazon Elastic Container Registry
**Action:**
1. Create ECR repository: `faxbot-api`
2. Configure lifecycle policy (keep last 10 images)
3. Enable scan on push for security vulnerabilities
4. Note the repository URI for CI/CD

### 3.3 Set Up S3 for File Storage
**Cost:** $5-15/month (depends on fax volume)
**AWS Service:** Amazon S3
**Action:**
1. Create bucket: `faxbot-net-artifacts-{random}`
2. **Enable versioning**
3. **Enable server-side encryption** with KMS
4. Create KMS key for encryption (HIPAA requirement)
5. Set lifecycle policy: delete files after 30 days
6. **Block all public access**
7. Create IAM role for ECS tasks to access this bucket

## Phase 4: Fax Service Provider

### 4.1 Set Up Phaxio Account
**Cost:** $0 setup + $0.07/page + $2/month per DID
**Link:** https://www.phaxio.com/
**Action:**
1. Create Phaxio account with business email
2. Complete business verification
3. **Request HIPAA BAA:** Email compliance@phaxio.com
4. **Disable document storage** in account settings (HIPAA requirement)
5. Enable 2FA on account
6. Get API credentials from console
7. Purchase phone number(s) for inbound faxes
8. Configure webhook URL: https://api.faxbot.net/phaxio-callback

**Phaxio Pricing:**
- Setup: Free
- Outbound: $0.07 per page
- Inbound: $0.07 per page  
- Phone numbers: $2/month per DID
- **Estimated:** $20-40/month for low volume

## Phase 5: Frontend Hosting

### 5.1 Set Up CloudFront + S3 for Frontend
**Cost:** $5-15/month
**AWS Services:** S3 + CloudFront
**Action:**
1. Create S3 bucket: `faxbot-net-frontend`
2. **Enable static website hosting**
3. **Enable server-side encryption**
4. Create CloudFront distribution:
   - Origin: S3 bucket
   - **Custom domain:** faxbot.net
   - **SSL certificate:** Use certificate from Phase 1.3
   - **HIPAA security headers** (see CloudFormation template)
5. Update DNS: Point faxbot.net to CloudFront distribution

## Phase 6: Monitoring and Compliance

### 6.1 Set Up CloudWatch Monitoring
**Cost:** $10-20/month
**AWS Service:** CloudWatch
**Action:**
1. Create custom dashboard for Faxbot metrics
2. Set up alarms:
   - API error rate > 5%
   - Database connections > 80%
   - S3 storage costs > $50/month
   - Failed fax rate > 10%
3. **Enable detailed monitoring** on all resources
4. Create SNS topic for alerts
5. Subscribe your email to alerts

### 6.2 Set Up Audit Logging
**Cost:** $5-10/month
**AWS Service:** CloudWatch Logs
**Action:**
1. Create log groups:
   - `/faxbot/api` (application logs)
   - `/faxbot/audit` (HIPAA audit trail)
   - `/faxbot/security` (security events)
2. **Enable log retention:** 90 days minimum for HIPAA
3. Set up log analysis for security events
4. **Export logs** to S3 for long-term storage

## Phase 7: Security and Compliance

### 7.1 Enable AWS Config
**Cost:** $10-15/month
**AWS Service:** AWS Config
**Action:**
1. Enable AWS Config in all regions you use
2. Enable compliance rules:
   - S3 bucket encryption
   - RDS encryption
   - VPC flow logs
   - Security group restrictions
3. Set up compliance dashboard
4. **Required for HIPAA audit trail**

### 7.2 Set Up AWS WAF
**Cost:** $5-10/month
**AWS Service:** AWS WAF
**Action:**
1. Create WAF Web ACL for CloudFront
2. Enable managed rules:
   - Core Rule Set
   - Known Bad Inputs
   - SQL Injection
   - Cross-site scripting (XSS)
3. Set rate limiting: 2000 requests per 5 minutes per IP
4. **Block non-US traffic** if only serving US customers

## Phase 8: Business Associate Agreements

### 8.1 Execute Required BAAs
**Cost:** $0 (legal requirement)
**Timeline:** Must complete BEFORE handling any PHI
**Action:**
1. **AWS BAA:**
   - Link: https://aws.amazon.com/compliance/hipaa-eligible-services/
   - Sign online through AWS console
   - Covers: EC2, RDS, S3, CloudFront, CloudWatch, etc.

2. **Phaxio BAA:**
   - Email: compliance@phaxio.com
   - Request HIPAA Business Associate Agreement
   - Timeline: Usually 1-2 weeks

3. **Domain/DNS BAA (if applicable):**
   - Namecheap: Check if BAA available for enterprise accounts
   - Alternative: Use AWS Route 53 (covered under AWS BAA)

## Phase 9: CI/CD and Deployment

### 9.1 Set Up GitHub Actions
**Cost:** Free (included with GitHub)
**Action:**
1. Create GitHub repository for faxbot.net (separate from open-source core)
2. Add secrets to repository:
   - AWS_ACCESS_KEY_ID
   - AWS_SECRET_ACCESS_KEY
   - AWS_REGION
   - S3_BUCKET
   - CLOUDFRONT_DISTRIBUTION_ID
3. Set up workflows (provided in frontend_plan_phases_4_8.md)

### 9.2 Configure Environment Variables
**Action:**
Set up production environment variables in ECS:
```env
# Core Faxbot Configuration
FAX_BACKEND=phaxio
DATABASE_URL=postgresql+psycopg2://faxbot:PASSWORD@your-rds-endpoint:5432/faxbot
PUBLIC_API_URL=https://api.faxbot.net

# HIPAA Security Settings
REQUIRE_API_KEY=true
ENFORCE_PUBLIC_HTTPS=true
AUDIT_LOG_ENABLED=true

# Phaxio Configuration  
PHAXIO_API_KEY=your_phaxio_key
PHAXIO_API_SECRET=your_phaxio_secret
PHAXIO_CALLBACK_URL=https://api.faxbot.net/phaxio-callback
PHAXIO_VERIFY_SIGNATURE=true

# Storage Configuration
STORAGE_BACKEND=s3
S3_BUCKET=faxbot-net-artifacts-{random}
S3_REGION=us-east-1
S3_KMS_KEY_ID=your-kms-key-id

# Inbound Configuration
INBOUND_ENABLED=true
INBOUND_RETENTION_DAYS=30
PHAXIO_INBOUND_VERIFY_SIGNATURE=true
```

## Cost Summary

### One-Time Costs:
- Domain registration (faxbot.net): $13
- SSL certificate: Free (AWS ACM)
- **Total One-Time:** ~$15

### Monthly Recurring Costs:
- **AWS RDS PostgreSQL** (db.t3.micro): $25
- **AWS ECS Fargate** (0.5 vCPU, 1GB): $35
- **AWS S3** (storage + requests): $10
- **AWS CloudFront** (CDN): $5
- **AWS CloudWatch** (monitoring): $15
- **AWS Config** (compliance): $10
- **AWS WAF** (security): $8
- **Phaxio** (fax service + 1 DID): $25
- **Domain renewal:** $1/month
- **Misc AWS services:** $15

**Total Monthly:** ~$149/month

### Additional Costs (Variable):
- **Fax usage:** $0.07 per page sent/received
- **Data transfer:** Minimal for low usage
- **Support:** AWS Business Support ($29/month minimum) - recommended for production

### Scaling Costs (Higher Usage):
- More ECS tasks: +$35/month each
- Larger RDS instance: db.t3.small (+$25/month)
- Additional Phaxio DIDs: +$2/month each
- Higher storage/bandwidth: Variable

## Pre-Launch Checklist

### Legal and Compliance:
- [ ] Business entity registered (LLC/Corp)
- [ ] Business insurance obtained
- [ ] Privacy policy and terms of service drafted
- [ ] HIPAA policies and procedures documented
- [ ] AWS BAA executed
- [ ] Phaxio BAA executed
- [ ] Incident response plan created

### Technical:
- [ ] Domain registered and DNS configured
- [ ] SSL certificates installed and validated
- [ ] Database encrypted and backed up
- [ ] Application deployed and health checks passing
- [ ] Monitoring and alerting configured
- [ ] Security scanning enabled
- [ ] Audit logging functional

### Testing:
- [ ] End-to-end fax sending tested
- [ ] Inbound fax receiving tested
- [ ] API key management tested
- [ ] Security headers validated
- [ ] HIPAA compliance verified
- [ ] Disaster recovery tested

**CRITICAL:** Do not handle any PHI until all BAAs are executed and compliance measures are verified.

