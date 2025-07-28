# Deployment Guide

## Production Deployment Options

### 1. Docker Deployment (Recommended)

#### Dockerfile
```dockerfile
FROM python:3.9-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install spaCy model
RUN python -m spacy download en_core_web_sm

# Copy application code
COPY . .

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Expose port
EXPOSE 5000

# Run application
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", "wsgi:app"]
```

#### docker-compose.yml
```yaml
version: '3.8'

services:
  app:
    build: .
    ports:
      - "5000:5000"
    environment:
      - FLASK_ENV=production
      - DATABASE_URL=postgresql://user:password@db:5432/nba_backend
      - OPENAI_API_KEY=${OPENAI_API_KEY}
    depends_on:
      - db
      - redis
    volumes:
      - ./logs:/app/logs

  db:
    image: postgres:13
    environment:
      - POSTGRES_DB=nba_backend
      - POSTGRES_USER=user
      - POSTGRES_PASSWORD=password
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./init.sql:/docker-entrypoint-initdb.d/init.sql

  redis:
    image: redis:6-alpine
    volumes:
      - redis_data:/data

  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf
      - ./ssl:/etc/nginx/ssl
    depends_on:
      - app

volumes:
  postgres_data:
  redis_data:
```

#### Build and Deploy
```bash
# Build and start services
docker-compose up -d --build

# Check logs
docker-compose logs -f app

# Scale application
docker-compose up -d --scale app=3
```

### 2. Cloud Deployment

#### AWS Elastic Beanstalk
```bash
# Install EB CLI
pip install awsebcli

# Initialize EB application
eb init nba-backend

# Create environment
eb create production

# Deploy
eb deploy
```

#### Requirements for EB
Create `.ebextensions/python.config`:
```yaml
option_settings:
  aws:elasticbeanstalk:container:python:
    WSGIPath: wsgi.py
  aws:elasticbeanstalk:application:environment:
    FLASK_ENV: production
    PYTHONPATH: /opt/python/current/app
```

#### Heroku Deployment
```bash
# Install Heroku CLI and login
heroku login

# Create app
heroku create nba-backend-prod

# Set environment variables
heroku config:set OPENAI_API_KEY=your_key
heroku config:set FLASK_ENV=production

# Deploy
git push heroku main

# Scale dynos
heroku ps:scale web=2
```

#### Procfile for Heroku
```
web: gunicorn wsgi:app
worker: python worker.py
```

### 3. VPS Deployment (Ubuntu)

#### Server Setup
```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python and dependencies
sudo apt install python3 python3-pip python3-venv nginx postgresql redis-server -y

# Create application user
sudo adduser --system --group --home /opt/nba-backend nbaapp
```

#### Application Setup
```bash
# Clone repository
sudo -u nbaapp git clone <repo-url> /opt/nba-backend
cd /opt/nba-backend

# Create virtual environment
sudo -u nbaapp python3 -m venv venv
sudo -u nbaapp ./venv/bin/pip install -r requirements.txt

# Install spaCy model
sudo -u nbaapp ./venv/bin/python -m spacy download en_core_web_sm
```

#### Systemd Service
Create `/etc/systemd/system/nba-backend.service`:
```ini
[Unit]
Description=NBA Backend API
After=network.target

[Service]
Type=exec
User=nbaapp
Group=nbaapp
WorkingDirectory=/opt/nba-backend
Environment=PATH=/opt/nba-backend/venv/bin
ExecStart=/opt/nba-backend/venv/bin/gunicorn --bind unix:/opt/nba-backend/nba-backend.sock --workers 4 wsgi:app
ExecReload=/bin/kill -s HUP $MAINPID
KillMode=mixed
TimeoutStopSec=5
PrivateTmp=true
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

#### Nginx Configuration
Create `/etc/nginx/sites-available/nba-backend`:
```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://unix:/opt/nba-backend/nba-backend.sock;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /static {
        alias /opt/nba-backend/static;
        expires 1y;
        add_header Cache-Control "public, immutable";
    }
}
```

#### Enable Services
```bash
# Enable and start services
sudo systemctl enable nba-backend
sudo systemctl start nba-backend

# Enable nginx site
sudo ln -s /etc/nginx/sites-available/nba-backend /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

## Production Configuration

### Environment Variables
```bash
# Application
FLASK_ENV=production
DEBUG=False
SECRET_KEY=secure-random-key-here

# Database
DATABASE_URL=postgresql://user:password@localhost/nba_backend

# LLM Configuration
OPENAI_API_KEY=your_openai_api_key
LLM_MODEL=gpt-4o-mini
LLM_TEMPERATURE=0
LLM_MAX_TOKENS=512
LLM_TIMEOUT=10.0
LLM_MAX_RETRIES=3

# Caching
REDIS_URL=redis://localhost:6379/0
CACHE_TYPE=redis

# Monitoring
SENTRY_DSN=your_sentry_dsn
LOG_LEVEL=INFO

# Security
CORS_ORIGINS=https://your-frontend-domain.com
RATE_LIMIT_ENABLED=True
API_RATE_LIMIT=100
```

### Production Settings
```python
# config.py
class ProductionConfig(Config):
    DEBUG = False
    TESTING = False
    
    # Database
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL')
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,
        'pool_recycle': 300,
        'pool_size': 10,
        'max_overflow': 20
    }
    
    # Caching
    CACHE_TYPE = 'redis'
    CACHE_REDIS_URL = os.environ.get('REDIS_URL')
    
    # Logging
    LOG_LEVEL = 'INFO'
    
    # Security
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    WTF_CSRF_ENABLED = True
```

## Database Setup

### PostgreSQL Setup
```bash
# Install PostgreSQL
sudo apt install postgresql postgresql-contrib

# Create database and user
sudo -u postgres psql
CREATE DATABASE nba_backend;
CREATE USER nba_user WITH PASSWORD 'secure_password';
GRANT ALL PRIVILEGES ON DATABASE nba_backend TO nba_user;
\q
```

### Database Migration
```bash
# Set database URL
export DATABASE_URL=postgresql://nba_user:secure_password@localhost/nba_backend

# Run migrations
python -m flask db upgrade

# Initialize data (if needed)
python scripts/init_data.py
```

### Database Backup Strategy
```bash
# Create backup script
#!/bin/bash
BACKUP_DIR="/opt/backups"
DATE=$(date +%Y%m%d_%H%M%S)
pg_dump nba_backend > "$BACKUP_DIR/nba_backend_$DATE.sql"

# Keep only last 7 days of backups
find "$BACKUP_DIR" -name "nba_backend_*.sql" -mtime +7 -delete
```

## Monitoring and Logging

### Application Monitoring
```python
# monitoring.py
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

sentry_sdk.init(
    dsn=os.environ.get('SENTRY_DSN'),
    integrations=[
        FlaskIntegration(),
        SqlalchemyIntegration(),
    ],
    traces_sample_rate=0.1,
    environment=os.environ.get('FLASK_ENV', 'production')
)
```

### Logging Configuration
```python
# logging_config.py
import logging
from logging.handlers import RotatingFileHandler, SysLogHandler

def setup_logging(app):
    # File logging
    file_handler = RotatingFileHandler(
        'logs/nba_backend.log',
        maxBytes=10*1024*1024,  # 10MB
        backupCount=10
    )
    file_handler.setLevel(logging.INFO)
    
    # Syslog for system integration
    syslog_handler = SysLogHandler(address='/dev/log')
    syslog_handler.setLevel(logging.WARNING)
    
    # Format
    formatter = logging.Formatter(
        '%(asctime)s %(name)s[%(process)d] %(levelname)s: %(message)s'
    )
    file_handler.setFormatter(formatter)
    syslog_handler.setFormatter(formatter)
    
    app.logger.addHandler(file_handler)
    app.logger.addHandler(syslog_handler)
    app.logger.setLevel(logging.INFO)
```

### Health Check Endpoint
```python
# health.py
from flask import Blueprint, jsonify
from app.services.database_service import DatabaseService

health_bp = Blueprint('health', __name__)

@health_bp.route('/health')
def health_check():
    try:
        # Check database connection
        DatabaseService.health_check()
        
        # Check external services
        openai_status = check_openai_connection()
        
        return jsonify({
            'status': 'healthy',
            'database': 'connected',
            'openai': 'connected' if openai_status else 'disconnected',
            'timestamp': datetime.utcnow().isoformat()
        })
    except Exception as e:
        return jsonify({
            'status': 'unhealthy',
            'error': str(e),
            'timestamp': datetime.utcnow().isoformat()
        }), 503
```

## Security Hardening

### SSL/TLS Configuration
```bash
# Install Certbot for Let's Encrypt
sudo apt install certbot python3-certbot-nginx

# Obtain SSL certificate
sudo certbot --nginx -d your-domain.com

# Auto-renewal
sudo crontab -e
# Add: 0 12 * * * /usr/bin/certbot renew --quiet
```

### Nginx Security Headers
```nginx
server {
    # ... existing configuration ...
    
    # Security headers
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options nosniff;
    add_header X-Frame-Options DENY;
    add_header X-XSS-Protection "1; mode=block";
    add_header Referrer-Policy strict-origin-when-cross-origin;
    
    # Rate limiting
    limit_req_zone $binary_remote_addr zone=api:10m rate=10r/s;
    
    location /api/ {
        limit_req zone=api burst=20 nodelay;
        # ... proxy configuration ...
    }
}
```

### Firewall Configuration
```bash
# Enable UFW
sudo ufw enable

# Allow SSH, HTTP, HTTPS
sudo ufw allow ssh
sudo ufw allow 'Nginx Full'

# Allow specific IPs for admin access
sudo ufw allow from YOUR_IP_ADDRESS to any port 22
```

## Performance Optimization

### Caching Strategy
```python
# cache.py
from flask_caching import Cache

cache = Cache()

# Cache configuration
CACHE_CONFIG = {
    'CACHE_TYPE': 'redis',
    'CACHE_REDIS_URL': os.environ.get('REDIS_URL'),
    'CACHE_DEFAULT_TIMEOUT': 300,
    'CACHE_KEY_PREFIX': 'nba_api:'
}

# Cache decorators
@cache.memoize(timeout=3600)
def get_player_season_stats(player_id, season):
    return PlayerService.get_season_stats(player_id, season)

# Cache invalidation
def invalidate_player_cache(player_id):
    cache.delete_memoized(get_player_season_stats, player_id)
```

### Database Connection Pooling
```python
# database.py
from sqlalchemy.pool import QueuePool

# Engine configuration
engine = create_engine(
    DATABASE_URL,
    poolclass=QueuePool,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=3600
)
```

### CDN Configuration
```bash
# Use CloudFlare or AWS CloudFront for static assets
# Configure cache headers for API responses

# Nginx caching for API responses
location /api/players {
    proxy_cache api_cache;
    proxy_cache_valid 200 1h;
    proxy_cache_key "$host$request_uri";
    # ... proxy configuration ...
}
```

## Backup and Recovery

### Automated Backup Script
```bash
#!/bin/bash
# backup.sh

BACKUP_DIR="/opt/backups"
DATE=$(date +%Y%m%d_%H%M%S)

# Database backup
pg_dump nba_backend | gzip > "$BACKUP_DIR/db_backup_$DATE.sql.gz"

# Application backup
tar -czf "$BACKUP_DIR/app_backup_$DATE.tar.gz" -C /opt nba-backend

# Upload to cloud storage (optional)
aws s3 cp "$BACKUP_DIR/db_backup_$DATE.sql.gz" s3://your-backup-bucket/

# Cleanup old backups
find "$BACKUP_DIR" -name "*.gz" -mtime +30 -delete
```

### Recovery Procedures
```bash
# Database recovery
gunzip -c backup_file.sql.gz | psql nba_backend

# Application recovery
tar -xzf app_backup.tar.gz -C /opt/
sudo systemctl restart nba-backend
```

## Scaling Considerations

### Horizontal Scaling
- Use **load balancer** (nginx, HAProxy, or cloud LB)
- **Stateless application design**
- **Shared session storage** (Redis)
- **Database read replicas**

### Vertical Scaling
- Monitor **resource usage**
- Increase **server capacity** as needed
- Optimize **database queries**
- Implement **connection pooling**

### Auto-scaling (Cloud)
```yaml
# AWS Auto Scaling Group configuration
AutoScalingGroupName: nba-backend-asg
MinSize: 2
MaxSize: 10
DesiredCapacity: 3
HealthCheckType: ELB
HealthCheckGracePeriod: 300
```

This deployment guide covers the essential aspects of deploying the NBA Backend API to production environments with proper security, monitoring, and scaling considerations.