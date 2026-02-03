# Railway Deployment Guide

This guide walks you through deploying the School Portal to Railway with MongoDB.

## Prerequisites

1. A [Railway](https://railway.app) account
2. A [Telegram Bot Token](https://t.me/BotFather) (create a bot and get the token)
3. (Optional) An [Anthropic API Key](https://console.anthropic.com/) for AI marking

---

## Step 1: Create a New Railway Project

1. Go to [railway.app](https://railway.app) and log in
2. Click **"New Project"**
3. Select **"Empty Project"**

---

## Step 2: Add MongoDB Database

1. In your project, click **"+ New"**
2. Select **"Database"** → **"Add MongoDB"**
3. Railway will create a MongoDB instance automatically
4. Click on the MongoDB service to see the connection details
5. Note: Railway automatically sets the `MONGO_URL` environment variable for linked services

---

## Step 3: Deploy the Web Application

### Option A: Deploy from GitHub

1. Push your code to a GitHub repository:
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git remote add origin https://github.com/YOUR_USERNAME/school-telegram-portal.git
   git push -u origin main
   ```

2. In Railway, click **"+ New"** → **"GitHub Repo"**
3. Select your repository
4. Railway will auto-detect and deploy

### Option B: Deploy from CLI

1. Install Railway CLI:
   ```bash
   npm install -g @railway/cli
   ```

2. Login and link:
   ```bash
   railway login
   railway link
   ```

3. Deploy:
   ```bash
   railway up
   ```

---

## Step 4: Configure Environment Variables

1. Click on your web service in Railway
2. Go to **"Variables"** tab
3. Add the following variables:

| Variable | Description | Example |
|----------|-------------|---------|
| `MONGO_URL` | Auto-set by Railway if MongoDB is linked | (auto) |
| `MONGODB_DB` | Database name | `school_portal` |
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token | `123456789:ABCdef...` |
| `FLASK_SECRET_KEY` | Random secret for sessions | Generate with: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `ADMIN_PASSWORD` | Password for admin login | `your-secure-password` |
| `ENCRYPTION_KEY` | Fernet key for API key encryption | Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `WEB_URL` | Your Railway app URL | `https://your-app.up.railway.app` |
| `ANTHROPIC_API_KEY` | (Optional) For AI marking | `sk-ant-api03-...` |
| `OPENAI_API_KEY` | (Optional) For textbook RAG embeddings | `sk-...` |

### Textbook RAG (ChromaDB) on Railway

The app can store a textbook PDF per module (RAG) so the AI tutor can answer from it. To enable this on Railway:

1. **Build**: The repo includes `nixpacks.toml` so Nixpacks installs GCC/gnumake before `pip install`. That allows `chromadb` to compile and the build to succeed. No extra step needed.
2. **Env**: Set `OPENAI_API_KEY` in your web service variables (used for embedding textbook chunks). Without it, textbook upload will show "Embeddings not available (set OPENAI_API_KEY)".
3. **Storage**: ChromaDB data is written under `data/chromadb`. On Railway the filesystem is ephemeral by default, so textbook data may be lost on redeploy unless you attach a [Railway Volume](https://docs.railway.app/reference/volumes) and set `CHROMA_DATA_PATH` to a path inside that volume.

### To Link MongoDB:
1. Click on your web service
2. Go to **"Variables"** tab
3. Click **"Add Reference Variable"**
4. Select your MongoDB service → `MONGO_URL`

---

## Step 5: Deploy the Telegram Bot (Separate Service)

The Telegram bot needs to run as a separate service:

1. In your Railway project, click **"+ New"** → **"GitHub Repo"** (same repo)
2. Rename this service to "telegram-bot"
3. Go to **"Settings"** tab
4. Under **"Deploy"**, set **Start Command** to:
   ```
   python bot.py
   ```
5. Add the same environment variables as the web app (or reference them)
6. **Important**: Link the `MONGO_URL` from MongoDB service

---

## Step 6: Generate Domain

1. Click on your web service
2. Go to **"Settings"** tab
3. Under **"Networking"**, click **"Generate Domain"**
4. Copy the generated URL (e.g., `your-app.up.railway.app`)
5. Update the `WEB_URL` environment variable with this URL

---

## Step 7: Verify Deployment

1. Visit your app URL: `https://your-app.up.railway.app`
2. You should see the student login page

### Test the Setup:

1. **Admin Login**: Go to `/admin/login` and use your `ADMIN_PASSWORD`
2. **Create a Teacher**: In admin dashboard, add a teacher
3. **Create Students**: Import students via JSON in admin dashboard
4. **Connect Telegram**: 
   - Message your bot on Telegram
   - Send `/start` to get your Telegram ID
   - Send `/verify TEACHER_ID` to link the teacher account

---

## Environment Variables Quick Reference

```bash
# Required
MONGO_URL=<auto-linked from Railway MongoDB>
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
FLASK_SECRET_KEY=your-64-character-secret-key
ADMIN_PASSWORD=your-secure-admin-password
ENCRYPTION_KEY=your-fernet-encryption-key
WEB_URL=https://your-app.up.railway.app

# Optional
MONGODB_DB=school_portal
ANTHROPIC_API_KEY=sk-ant-api03-...
FLASK_DEBUG=false
```

---

## Generate Secret Keys

Run these commands locally to generate secure keys:

```bash
# Flask Secret Key
python -c "import secrets; print(secrets.token_hex(32))"

# Encryption Key (Fernet)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## Troubleshooting

### App won't start
- Check the **"Deployments"** tab for build logs
- Ensure all required environment variables are set
- Verify MongoDB is linked correctly

### MongoDB connection errors
- Make sure `MONGO_URL` is set (reference from MongoDB service)
- Check if MongoDB service is running

### Telegram bot not responding
- Verify `TELEGRAM_BOT_TOKEN` is correct
- Check bot service logs in Railway
- Ensure the bot service is running (not the web service)

### AI Feedback not working
- Add `ANTHROPIC_API_KEY` to environment variables
- Or configure per-teacher API keys in Teacher Settings

---

## Sample Data for Testing

### Import Students (via Admin Dashboard)

```json
[
  {
    "student_id": "S001",
    "name": "John Smith",
    "class": "10A",
    "password": "student123",
    "teachers": ["T001"]
  },
  {
    "student_id": "S002",
    "name": "Jane Doe",
    "class": "10A",
    "password": "student123",
    "teachers": ["T001"]
  }
]
```

### Add Teacher (via Admin Dashboard)

- **Teacher ID**: T001
- **Name**: Mr. Teacher
- **Password**: teacher123
- **Subjects**: Mathematics, Science

---

## Architecture on Railway

```
Railway Project
├── MongoDB (Database)
│   └── school_portal database
├── Web Service (Flask App)
│   └── gunicorn app:app
└── Telegram Bot Service
    └── python bot.py
```

Both services share the same MongoDB database and environment variables.

---

## Cost Estimation

Railway offers:
- **Free Tier**: $5/month credit (enough for small usage)
- **Hobby Plan**: $5/month
- **Pro Plan**: $20/month

MongoDB on Railway is included in these plans based on usage.

---

## Support

If you encounter issues:
1. Check Railway's deployment logs
2. Review the app's error logs
3. Ensure all environment variables are correctly set
