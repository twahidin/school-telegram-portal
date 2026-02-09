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
| `PINECONE_API_KEY` | (Optional) For textbook RAG vector storage | `pcsk_...` |
| `PINECONE_INDEX_NAME` | (Optional) Your Pinecone index name | `school-portal` |
| `RATELIMIT_STORAGE_URI` | (Optional) Redis URI for rate limits; removes in-memory warning | `redis://default:...@host:port` |

### Textbook RAG (Pinecone) on Railway

The app can store a textbook PDF per module (RAG) so the AI tutor can answer from it. This uses **Pinecone** (a hosted vector database) to avoid memory issues on Railway.

**Setup steps:**

1. **Create a free Pinecone account** at [pinecone.io](https://www.pinecone.io/)

2. **Create an index:**
   - In the Pinecone console, click **Create Index**
   - **Name**: e.g. `school-portal` (you'll use this as `PINECONE_INDEX_NAME`)
   - **Dimensions**: `1536` (required for OpenAI text-embedding-3-small)
   - **Metric**: `cosine`
   - **Serverless** (free tier available) or **Pod-based**
   - Click **Create Index**

3. **Get your API key:**
   - In Pinecone console → **API Keys** → copy your key

4. **Set environment variables** in Railway:
   - `PINECONE_API_KEY` = your Pinecone API key
   - `PINECONE_INDEX_NAME` = your index name (e.g. `school-portal`)
   - `OPENAI_API_KEY` = your OpenAI key (for generating embeddings)

**Notes:**
- Pinecone free tier includes 1 index with 100K vectors (plenty for textbooks)
- Data persists in Pinecone cloud (no Railway volume needed)
- Each module's textbook is stored in a separate namespace within the index
- **PDF extraction uses PyPDF2 by default** - lightweight and works on Railway. Best for text-based PDFs (digital documents, typed content).
- **502 / Upload failed / SIGKILL (OOM):** If you get worker SIGKILL, try a smaller PDF (e.g. one chapter, < 5 MB) or upgrade Railway memory.
- **Anthropic Vision extraction (NOT recommended for Railway):** Setting `USE_ANTHROPIC_VISION_FOR_PDF=true` enables image-based extraction for scanned PDFs, BUT this uses `pdf2image` which is very memory-intensive and will likely cause OOM on Railway's limited memory. Only enable this if you have upgraded Railway resources or are running locally. Note: AI marking (reading student handwriting) is separate and works fine - it sends images directly to Claude without local conversion.

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

## Auto-deploy from GitHub (pushes not triggering deploys)

If pushing to GitHub does **not** trigger a Railway deploy:

1. **Confirm the service is from GitHub**
   - In Railway, open your **web service** (or the service you expect to deploy).
   - In **Settings**, check that **Source** is **GitHub** and the correct repo/branch is shown.

2. **Turn on deployments from GitHub**
   - In the service, go to **Settings** → **Source** (or **Deploy**).
   - Ensure **Deploy on push** (or **Auto-deploy**) is **enabled** for your branch (e.g. `main`).

3. **Reconnect GitHub if needed**
   - If the repo was connected long ago or you see "Disconnected" / webhook errors:
     - **Settings** → **Source** → **Disconnect**, then **Connect Repo** again and choose the same GitHub repo and branch.
   - Railway will reinstall its GitHub App / webhook; new pushes should trigger builds.

4. **Check branch**
   - Railway only auto-deploys the branch you connected (usually `main`). Pushes to other branches will not deploy unless you add them or use a different service.

5. **Verify in GitHub**
   - Repo → **Settings** → **Integrations** (or **Webhooks**): you should see **Railway** or a webhook pointing to Railway. If it’s missing, reconnect the repo from Railway (step 3).

After fixing, push a small commit to `main`; a new deployment should appear in Railway’s **Deployments** tab.

---

## Support

If you encounter issues:
1. Check Railway's deployment logs
2. Review the app's error logs
3. Ensure all environment variables are correctly set
